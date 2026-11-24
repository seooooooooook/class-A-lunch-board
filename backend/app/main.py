"""오늘의 급식 평가판 - FastAPI 백엔드

실행: (backend 폴더에서)  uvicorn app.main:app --reload
화면: http://localhost:8000  (학생)   http://localhost:8000/admin.html  (선생님)
API 문서: http://localhost:8000/docs
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import sync
from .db import connect, get_setting, init_db, set_setting
from .textfilter import MESSAGES, check_text

BACKEND_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"
load_dotenv(BACKEND_DIR / ".env")  # 나이스 인증키, 선생님 비밀번호

log = logging.getLogger("lunch")
KST = ZoneInfo("Asia/Seoul")
REPORTS_TO_HIDE = 3
NICKNAME_MAX = 10
COMMENT_MAX = 50
DateStr = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]


def today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    task = None
    if os.environ.get("NEIS_AUTO_SYNC", "1") != "0":
        task = asyncio.create_task(sync.scheduler())  # 매일 9시 나이스 자동 불러오기
    yield
    if task:
        task.cancel()


app = FastAPI(title="오늘의 급식 평가판", lifespan=lifespan)


@app.middleware("http")
async def no_index(request, call_next):
    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow"  # 검색 엔진에 나오지 않게
    # 브라우저가 예전 화면 파일을 붙잡고 있지 않게, 열 때마다 바뀌었는지 확인하게 해요
    response.headers.setdefault("Cache-Control", "no-cache")
    return response


# ---------------------------------------------------------------- 공통 도우미

def get_db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


Db = Annotated[sqlite3.Connection, Depends(get_db)]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def fail(status: int, message: str):
    raise HTTPException(status_code=status, detail=message)


def extra_banned_words(conn) -> list[str]:
    return json.loads(get_setting(conn, "banned_words", "[]"))


def session_student(db: Db, authorization: Annotated[str | None, Header()] = None) -> sqlite3.Row:
    """로그인한 학생 (예전 방식으로 들어와 아직 계정을 만들지 않은 학생도 포함)."""
    token = bearer(authorization)
    row = token and db.execute(
        """SELECT s.* FROM student_sessions ss JOIN students s ON s.id = ss.student_id
           WHERE ss.token_hash = ?""",
        (hash_token(token),),
    ).fetchone()
    if not row:
        fail(401, "다시 로그인해 주세요.")
    return row


def current_student(student: Annotated[sqlite3.Row, Depends(session_student)]) -> sqlite3.Row:
    if not student["login_id"]:
        fail(403, "먼저 아이디와 비밀번호를 만들어 주세요.")
    return student


def current_teacher(db: Db, authorization: Annotated[str | None, Header()] = None) -> None:
    token = bearer(authorization)
    row = token and db.execute("SELECT 1 FROM teacher_sessions WHERE token_hash = ?", (hash_token(token),)).fetchone()
    if not row:
        fail(401, "선생님 로그인이 필요해요.")


Student = Annotated[sqlite3.Row, Depends(current_student)]
SessionStudent = Annotated[sqlite3.Row, Depends(session_student)]
Teacher = Annotated[None, Depends(current_teacher)]


# ---------------------------------------------------------------- 학생: 회원가입 · 로그인 · 로그아웃

LOGIN_ID_RE = re.compile(r"^[a-z0-9]{4,12}$")
REAL_NAME_RE = re.compile(r"^[가-힣A-Za-z]{2,10}$")
CLASS_RE = re.compile(r"^([1-6])\s*[-–—~.]\s*(\d{1,2})$")
PASSWORD_MIN, PASSWORD_MAX = 4, 30
PBKDF2_ROUNDS = 200_000
LOGIN_FAILS_TO_LOCK, LOGIN_LOCK_SECONDS = 5, 60
_login_fails: dict[str, tuple[int, float]] = {}  # 아이디 → (틀린 횟수, 잠금이 풀리는 시각)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str | None) -> bool:
    try:
        _, rounds, salt, digest = (stored or "").split("$")
        mine = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds))
    except ValueError:
        return False
    return hmac.compare_digest(mine.hex(), digest)


def parse_class(text: str) -> tuple[int, int]:
    """'5-1', '5 - 1' → (5, 1). 선생님이 받을 반을 정해 두었으면 그 반만 받아요."""
    m = CLASS_RE.match((text or "").strip())
    if not m or not 1 <= int(m.group(2)) <= 20:
        fail(400, "학년-반을 '5-1'처럼 숫자로 써 주세요.")
    return int(m.group(1)), int(m.group(2))


def class_label(grade, class_no) -> str | None:
    return f"{grade}-{class_no}" if grade and class_no else None


def check_password_rule(password: str) -> None:
    if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        fail(400, f"비밀번호는 {PASSWORD_MIN}~{PASSWORD_MAX}글자로 써 주세요.")


def clean_real_name(name: str) -> str:
    name = "".join((name or "").split())
    if not REAL_NAME_RE.match(name):
        fail(400, "진짜 이름을 한글 2~10글자로 써 주세요.")
    return name


def clean_nickname(db, nickname: str, real_name: str | None = None) -> str:
    nickname = " ".join((nickname or "").split())
    if not 1 <= len(nickname) <= NICKNAME_MAX:
        fail(400, f"닉네임은 1~{NICKNAME_MAX}글자로 써 주세요.")
    problem = check_text(nickname, extra_banned_words(db))
    if problem:
        fail(400, MESSAGES[problem])
    if real_name and (real_name in "".join(nickname.split()) or (len(real_name) >= 3 and real_name[1:] in nickname)):
        fail(400, "닉네임에 진짜 이름을 쓰지 말아 주세요. 친구들이 볼 수 있어요.")
    return nickname


def clean_login_id(db, login_id: str, me: int | None = None) -> str:
    login_id = (login_id or "").strip().lower()
    if not LOGIN_ID_RE.match(login_id):
        fail(400, "아이디는 영어 소문자와 숫자로 4~12글자로 써 주세요.")
    row = db.execute("SELECT id FROM students WHERE login_id = ?", (login_id,)).fetchone()
    if row and row["id"] != me:
        fail(409, "이미 있는 아이디예요. 다른 아이디를 골라 주세요.")
    return login_id


def allowed_classes(db) -> list[str]:
    return json.loads(get_setting(db, "allowed_classes", "[]"))


def check_class(db, text: str) -> tuple[int, int]:
    grade, class_no = parse_class(text)
    allowed = allowed_classes(db)
    if allowed and f"{grade}-{class_no}" not in allowed:
        fail(403, "우리 반 학년-반이 아니에요. 선생님께 다시 확인해 주세요.")
    return grade, class_no


def new_session(db, student_id: int) -> str:
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO student_sessions(token_hash, student_id) VALUES(?, ?)", (hash_token(token), student_id))
    return token


def me_info(row) -> dict:
    """학생 본인에게 보내는 정보. 진짜 이름은 넣지 않아요 (같은 기기를 친구가 볼 수도 있어서)."""
    return {
        "nickname": row["nickname"],
        "class_name": class_label(row["grade"], row["class_no"]),
        "login_id": row["login_id"],
        "needs_account": not row["login_id"],
    }


class SignupIn(BaseModel):
    class_name: str = Field(max_length=10)
    real_name: str = Field(max_length=20)
    nickname: str = Field(max_length=30)
    login_id: str = Field(max_length=30)
    password: str = Field(max_length=100)


@app.post("/api/signup")
def signup(body: SignupIn, db: Db):
    grade, class_no = check_class(db, body.class_name)
    real_name = clean_real_name(body.real_name)
    nickname = clean_nickname(db, body.nickname, real_name)
    login_id = clean_login_id(db, body.login_id)
    check_password_rule(body.password)
    if db.execute("SELECT 1 FROM students WHERE nickname = ?", (nickname,)).fetchone():
        fail(409, "이미 다른 친구가 쓰는 닉네임이에요. 다른 닉네임을 골라 주세요.")
    cur = db.execute(
        """INSERT INTO students(nickname, login_id, password_hash, real_name, grade, class_no)
           VALUES(?, ?, ?, ?, ?, ?)""",
        (nickname, login_id, hash_password(body.password), real_name, grade, class_no),
    )
    token = new_session(db, cur.lastrowid)
    row = db.execute("SELECT * FROM students WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"token": token, **me_info(row)}


class StudentLoginIn(BaseModel):
    login_id: str = Field(max_length=30)
    password: str = Field(max_length=100)


@app.post("/api/login")
def student_login(body: StudentLoginIn, db: Db):
    login_id = body.login_id.strip().lower()
    fails, locked_until = _login_fails.get(login_id, (0, 0.0))
    if locked_until > time.monotonic():
        fail(429, "비밀번호를 여러 번 틀렸어요. 1분 뒤에 다시 해 주세요.")
    row = db.execute("SELECT * FROM students WHERE login_id = ?", (login_id,)).fetchone()
    if not row or not check_password(body.password, row["password_hash"]):
        fails += 1
        _login_fails[login_id] = (0, time.monotonic() + LOGIN_LOCK_SECONDS) if fails >= LOGIN_FAILS_TO_LOCK else (fails, 0.0)
        fail(403, "아이디나 비밀번호가 맞지 않아요. 잊어버렸으면 선생님께 말해 주세요.")
    _login_fails.pop(login_id, None)
    return {"token": new_session(db, row["id"]), **me_info(row)}


@app.post("/api/logout")
def student_logout(_: SessionStudent, db: Db, authorization: Annotated[str | None, Header()] = None):
    db.execute("DELETE FROM student_sessions WHERE token_hash = ?", (hash_token(bearer(authorization)),))
    return {"ok": True}


@app.get("/api/me")
def me(student: SessionStudent):
    return me_info(student)


class AccountIn(BaseModel):
    class_name: str = Field(max_length=10)
    real_name: str = Field(max_length=20)
    login_id: str = Field(max_length=30)
    password: str = Field(max_length=100)


@app.post("/api/me/account")
def make_account(body: AccountIn, student: SessionStudent, db: Db):
    """예전에 입장 코드로 들어온 학생이 지금 닉네임 그대로 아이디·비밀번호를 만들어요. 별점·후기는 그대로예요."""
    if student["login_id"]:
        fail(409, "이미 계정이 있어요.")
    grade, class_no = check_class(db, body.class_name)
    real_name = clean_real_name(body.real_name)
    login_id = clean_login_id(db, body.login_id, student["id"])
    check_password_rule(body.password)
    db.execute(
        """UPDATE students SET login_id = ?, password_hash = ?, real_name = ?, grade = ?, class_no = ?
           WHERE id = ?""",
        (login_id, hash_password(body.password), real_name, grade, class_no, student["id"]),
    )
    row = db.execute("SELECT * FROM students WHERE id = ?", (student["id"],)).fetchone()
    return me_info(row)


# ---------------------------------------------------------------- 학생: 오늘의 급식

@app.get("/api/days/{date}")
def day(date: DateStr, student: Student, db: Db):
    """그날 메뉴와 메뉴별 평균, 후기 목록, 내 평가를 한 번에 보내요."""
    menus = db.execute("SELECT id, name, ord FROM menus WHERE date = ? ORDER BY ord, id", (date,)).fetchall()
    reviews = db.execute(
        """SELECT r.id, r.menu_id, r.student_id, s.nickname, r.stars, r.comment, r.hidden, r.updated_at
           FROM reviews r JOIN students s ON s.id = r.student_id
           JOIN menus m ON m.id = r.menu_id
           WHERE m.date = ? ORDER BY r.updated_at DESC, r.id DESC""",
        (date,),
    ).fetchall()
    likes = {}
    for row in db.execute(
        """SELECT l.review_id, COUNT(*) AS n, MAX(l.student_id = ?) AS mine
           FROM review_likes l JOIN reviews r ON r.id = l.review_id JOIN menus m ON m.id = r.menu_id
           WHERE m.date = ? GROUP BY l.review_id""",
        (student["id"], date),
    ):
        likes[row["review_id"]] = (row["n"], bool(row["mine"]))
    events = db.execute("SELECT name, no_school FROM school_events WHERE date = ? ORDER BY id", (date,)).fetchall()
    result = []
    for m in menus:
        mine = None
        visible = []
        for r in reviews:
            if r["menu_id"] != m["id"]:
                continue
            is_mine = r["student_id"] == student["id"]
            if is_mine:
                mine = {"stars": r["stars"], "comment": r["comment"], "hidden": bool(r["hidden"])}
            if r["hidden"]:
                continue  # 숨긴 후기는 다른 친구에게 보내지 않아요
            like_count, liked = likes.get(r["id"], (0, False))
            visible.append({
                "id": r["id"], "nickname": r["nickname"], "stars": r["stars"],
                "comment": r["comment"], "mine": is_mine, "likes": like_count, "liked": liked,
            })
        stars = [v["stars"] for v in visible]
        result.append({
            "id": m["id"],
            "name": m["name"],
            "avg": round(sum(stars) / len(stars), 1) if stars else None,
            "count": len(stars),
            "mine": mine,
            "reviews": [v for v in visible if v["comment"]],
        })
    return {
        "date": date,
        "today": today_kst(),
        "menus": result,
        "events": [{"name": e["name"], "no_school": bool(e["no_school"])} for e in events],
    }


class ReviewIn(BaseModel):
    stars: int = Field(ge=1, le=5)
    comment: str = ""


def menu_for_today(db, menu_id: int) -> sqlite3.Row:
    menu = db.execute("SELECT * FROM menus WHERE id = ?", (menu_id,)).fetchone()
    if not menu:
        fail(404, "없는 메뉴예요.")
    if menu["date"] != today_kst():
        fail(403, "평가는 그날에만 할 수 있어요.")
    return menu


@app.put("/api/menus/{menu_id}/review")
def save_review(menu_id: int, body: ReviewIn, student: Student, db: Db):
    menu_for_today(db, menu_id)
    comment = body.comment.strip()
    if len(comment) > COMMENT_MAX:
        fail(400, f"후기는 {COMMENT_MAX}자까지 쓸 수 있어요.")
    if comment:
        problem = check_text(comment, extra_banned_words(db))
        if problem:
            fail(400, MESSAGES[problem])
    # 처음이면 새로 만들고, 다시 누르면 점수와 글만 바꿔요 (숨김·신고 수는 그대로).
    db.execute(
        """INSERT INTO reviews(menu_id, student_id, stars, comment) VALUES(?, ?, ?, ?)
           ON CONFLICT(menu_id, student_id) DO UPDATE SET
             stars = excluded.stars, comment = excluded.comment, updated_at = datetime('now')""",
        (menu_id, student["id"], body.stars, comment),
    )
    return {"ok": True}


@app.delete("/api/menus/{menu_id}/review")
def delete_review(menu_id: int, student: Student, db: Db):
    menu_for_today(db, menu_id)
    db.execute("DELETE FROM reviews WHERE menu_id = ? AND student_id = ?", (menu_id, student["id"]))
    return {"ok": True}


@app.post("/api/reviews/{review_id}/report")
def report(review_id: int, student: Student, db: Db):
    review = db.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    if not review:
        fail(404, "이미 지워진 후기예요.")
    if review["student_id"] == student["id"]:
        fail(400, "내가 쓴 후기는 신고할 수 없어요.")
    try:
        db.execute("INSERT INTO reports(review_id, student_id) VALUES(?, ?)", (review_id, student["id"]))
    except sqlite3.IntegrityError:
        fail(409, "이미 신고한 후기예요.")
    count = review["report_count"] + 1
    hide = count >= REPORTS_TO_HIDE and not review["hidden"]
    if hide:
        db.execute("UPDATE reviews SET report_count = ?, hidden = 1, hidden_by = 'reports' WHERE id = ?", (count, review_id))
    else:
        db.execute("UPDATE reviews SET report_count = ? WHERE id = ?", (count, review_id))
    return {"report_count": count, "hidden": hide or bool(review["hidden"])}


def likeable_review(db, review_id: int, student) -> sqlite3.Row:
    review = db.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    if not review or review["hidden"]:
        fail(404, "볼 수 없는 후기예요.")
    if review["student_id"] == student["id"]:
        fail(400, "내 후기에는 좋아요를 누를 수 없어요.")
    return review


def like_count(db, review_id: int) -> int:
    return db.execute("SELECT COUNT(*) FROM review_likes WHERE review_id = ?", (review_id,)).fetchone()[0]


@app.put("/api/reviews/{review_id}/like")
def like(review_id: int, student: Student, db: Db):
    likeable_review(db, review_id, student)
    db.execute("INSERT OR IGNORE INTO review_likes(review_id, student_id) VALUES(?, ?)", (review_id, student["id"]))
    return {"likes": like_count(db, review_id), "liked": True}


@app.delete("/api/reviews/{review_id}/like")
def unlike(review_id: int, student: Student, db: Db):
    db.execute("DELETE FROM review_likes WHERE review_id = ? AND student_id = ?", (review_id, student["id"]))
    return {"likes": like_count(db, review_id), "liked": False}


# ---------------------------------------------------------------- 선생님: 로그인

class LoginIn(BaseModel):
    password: str


@app.post("/api/teacher/login")
def teacher_login(body: LoginIn, db: Db):
    expected = os.environ.get("TEACHER_PASSWORD", "")
    if len(expected) < 4:
        fail(503, "backend/.env 에 TEACHER_PASSWORD 를 4글자 이상으로 넣고 서버를 다시 켜 주세요.")
    if not hmac.compare_digest(body.password.encode(), expected.encode()):
        fail(403, "비밀번호가 맞지 않아요.")
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO teacher_sessions(token_hash) VALUES(?)", (hash_token(token),))
    return {"token": token}


@app.post("/api/teacher/logout")
def teacher_logout(_: Teacher, db: Db, authorization: Annotated[str | None, Header()] = None):
    db.execute("DELETE FROM teacher_sessions WHERE token_hash = ?", (hash_token(bearer(authorization)),))
    return {"ok": True}


# ---------------------------------------------------------------- 선생님: 메뉴

@app.get("/api/teacher/menus")
def teacher_menus(date: Annotated[str, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")], _: Teacher, db: Db):
    rows = db.execute(
        """SELECT m.id, m.name, m.source, COUNT(r.id) AS review_count
           FROM menus m LEFT JOIN reviews r ON r.menu_id = m.id
           WHERE m.date = ? GROUP BY m.id ORDER BY m.ord, m.id""",
        (date,),
    ).fetchall()
    return {"date": date, "menus": [dict(r) for r in rows]}


class MenuItem(BaseModel):
    id: int | None = None
    name: str = Field(min_length=1, max_length=30)
    source: Literal["neis", "manual"] = "manual"


class MenusIn(BaseModel):
    menus: list[MenuItem] = Field(max_length=20)


@app.put("/api/teacher/menus/{date}")
def save_menus(date: DateStr, body: MenusIn, _: Teacher, db: Db):
    """그날 메뉴 목록을 통째로 저장해요. id 가 있는 메뉴는 이름만 바뀌고 후기는 그대로 남아요."""
    existing = {r["id"] for r in db.execute("SELECT id FROM menus WHERE date = ?", (date,))}
    keep = {m.id for m in body.menus if m.id is not None}
    if not keep <= existing:
        fail(400, "다른 날짜의 메뉴가 섞여 있어요.")
    for menu_id in existing - keep:
        db.execute("DELETE FROM menus WHERE id = ?", (menu_id,))
    for order, m in enumerate(body.menus):
        name = m.name.strip()
        if m.id is None:
            db.execute("INSERT INTO menus(date, name, ord, source) VALUES(?, ?, ?, ?)", (date, name, order, m.source))
        else:
            db.execute("UPDATE menus SET name = ?, ord = ?, source = ? WHERE id = ?", (name, order, m.source, m.id))
    return teacher_menus(date, None, db)


@app.get("/api/teacher/neis-status")
def neis_status(_: Teacher):
    """나이스 자동 불러오기 결과. 불러오기는 서버가 매일 아침 스스로 해요."""
    return {"last": sync.last_status(), "next": sync.next_scheduled(),
            "hour": sync.sync_hour(), "days": sync.sync_days()}


# ---------------------------------------------------------------- 선생님: 후기 관리

@app.get("/api/teacher/reviews")
def teacher_reviews(
    _: Teacher, db: Db,
    mode: Literal["flagged", "date"] = "flagged",
    date: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
):
    sql = """SELECT r.id, m.date, m.name AS menu, s.nickname, s.real_name,
                    CASE WHEN s.grade IS NOT NULL THEN s.grade || '-' || s.class_no END AS class_name,
                    r.stars, r.comment,
                    r.report_count, r.hidden, r.hidden_by, r.updated_at
             FROM reviews r JOIN menus m ON m.id = r.menu_id JOIN students s ON s.id = r.student_id"""
    if mode == "date":
        rows = db.execute(sql + " WHERE m.date = ? ORDER BY r.updated_at DESC", (date or today_kst(),)).fetchall()
    else:
        rows = db.execute(sql + " WHERE r.hidden = 1 OR r.report_count > 0 ORDER BY r.updated_at DESC").fetchall()
    return {"reviews": [{**dict(r), "hidden": bool(r["hidden"])} for r in rows]}


class HiddenIn(BaseModel):
    hidden: bool


@app.patch("/api/teacher/reviews/{review_id}")
def set_hidden(review_id: int, body: HiddenIn, _: Teacher, db: Db):
    if body.hidden:
        cur = db.execute("UPDATE reviews SET hidden = 1, hidden_by = 'teacher' WHERE id = ?", (review_id,))
    else:  # 되살리면 신고 수도 0으로
        cur = db.execute("UPDATE reviews SET hidden = 0, hidden_by = NULL, report_count = 0 WHERE id = ?", (review_id,))
        db.execute("DELETE FROM reports WHERE review_id = ?", (review_id,))
    if cur.rowcount == 0:
        fail(404, "없는 후기예요.")
    return {"ok": True}


@app.delete("/api/teacher/reviews/{review_id}")
def teacher_delete_review(review_id: int, _: Teacher, db: Db):
    db.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
    return {"ok": True}


# ---------------------------------------------------------------- 선생님: 금지어

class WordIn(BaseModel):
    word: str = Field(min_length=1, max_length=20)


@app.get("/api/teacher/banned-words")
def get_words(_: Teacher, db: Db):
    return {"words": extra_banned_words(db)}


@app.post("/api/teacher/banned-words")
def add_word(body: WordIn, _: Teacher, db: Db):
    words = extra_banned_words(db)
    word = body.word.strip()
    if word and word not in words:
        words.append(word)
        set_setting(db, "banned_words", json.dumps(words, ensure_ascii=False))
    return {"words": words}


@app.delete("/api/teacher/banned-words/{word}")
def remove_word(word: str, _: Teacher, db: Db):
    words = [w for w in extra_banned_words(db) if w != word]
    set_setting(db, "banned_words", json.dumps(words, ensure_ascii=False))
    return {"words": words}


# ---------------------------------------------------------------- 선생님: 우리 반과 학생

class ClassesIn(BaseModel):
    classes: list[str] = Field(max_length=30)


@app.get("/api/teacher/classes")
def get_classes(_: Teacher, db: Db):
    return {"classes": allowed_classes(db)}


@app.put("/api/teacher/classes")
def save_classes(body: ClassesIn, _: Teacher, db: Db):
    """가입을 받을 학년-반. 비워 두면 1~6학년 어느 반이든 받아요."""
    labels = []
    for text in body.classes:
        if not text.strip():
            continue
        label = "-".join(map(str, parse_class(text)))
        if label not in labels:
            labels.append(label)
    set_setting(db, "allowed_classes", json.dumps(labels))
    return {"classes": labels}


@app.get("/api/teacher/students")
def students(_: Teacher, db: Db):
    """선생님만 보는 학생 목록. 여기에만 진짜 이름이 나와요."""
    rows = db.execute(
        """SELECT id, nickname, login_id, real_name, grade, class_no, created_at FROM students
           ORDER BY grade IS NULL, grade, class_no, real_name, nickname"""
    ).fetchall()
    return {"students": [{
        "id": r["id"], "nickname": r["nickname"], "login_id": r["login_id"], "real_name": r["real_name"],
        "class_name": class_label(r["grade"], r["class_no"]), "has_account": bool(r["login_id"]),
        "created_at": r["created_at"],
    } for r in rows]}


class PasswordIn(BaseModel):
    password: str = Field(max_length=100)


@app.put("/api/teacher/students/{student_id}/password")
def reset_password(student_id: int, body: PasswordIn, _: Teacher, db: Db):
    """비밀번호를 잊은 학생에게 새 비밀번호를 정해 줘요. 다른 기기의 로그인은 모두 풀려요."""
    check_password_rule(body.password)
    cur = db.execute("UPDATE students SET password_hash = ? WHERE id = ? AND login_id IS NOT NULL",
                     (hash_password(body.password), student_id))
    if cur.rowcount == 0:
        fail(404, "계정이 있는 학생이 아니에요.")
    db.execute("DELETE FROM student_sessions WHERE student_id = ?", (student_id,))
    return {"ok": True}


class NicknameIn(BaseModel):
    nickname: str = Field(min_length=1, max_length=NICKNAME_MAX)


@app.patch("/api/teacher/students/{student_id}")
def rename_student(student_id: int, body: NicknameIn, _: Teacher, db: Db):
    nickname = " ".join(body.nickname.split())
    try:
        cur = db.execute("UPDATE students SET nickname = ? WHERE id = ?", (nickname, student_id))
    except sqlite3.IntegrityError:
        fail(409, "이미 다른 학생이 쓰는 닉네임이에요.")
    if cur.rowcount == 0:
        fail(404, "없는 학생이에요.")
    return {"ok": True}


@app.delete("/api/teacher/students/{student_id}")
def remove_student(student_id: int, _: Teacher, db: Db):
    """학생을 내보내요. 그 학생의 별점·후기도 같이 지워져요."""
    db.execute("DELETE FROM students WHERE id = ?", (student_id,))
    return {"ok": True}


# ---------------------------------------------------------------- 화면 파일

app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
