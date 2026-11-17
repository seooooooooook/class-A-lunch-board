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
import secrets
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


def current_student(db: Db, authorization: Annotated[str | None, Header()] = None) -> sqlite3.Row:
    token = bearer(authorization)
    row = token and db.execute("SELECT * FROM students WHERE token_hash = ?", (hash_token(token),)).fetchone()
    if not row:
        fail(401, "반 입장 코드를 넣고 다시 들어와 주세요.")
    return row


def current_teacher(db: Db, authorization: Annotated[str | None, Header()] = None) -> None:
    token = bearer(authorization)
    row = token and db.execute("SELECT 1 FROM teacher_sessions WHERE token_hash = ?", (hash_token(token),)).fetchone()
    if not row:
        fail(401, "선생님 로그인이 필요해요.")


Student = Annotated[sqlite3.Row, Depends(current_student)]
Teacher = Annotated[None, Depends(current_teacher)]


# ---------------------------------------------------------------- 학생: 입장

class JoinIn(BaseModel):
    code: str
    nickname: str


@app.post("/api/join")
def join(body: JoinIn, db: Db):
    code = body.code.strip().upper()
    nickname = " ".join(body.nickname.split())
    if not code:
        fail(400, "반 입장 코드를 넣어 주세요.")
    if not 1 <= len(nickname) <= NICKNAME_MAX:
        fail(400, f"닉네임은 1~{NICKNAME_MAX}글자로 써 주세요.")
    problem = check_text(nickname, extra_banned_words(db))
    if problem:
        fail(400, MESSAGES[problem])

    join_code = (get_setting(db, "join_code") or "").upper()
    if not join_code:
        fail(409, "선생님이 아직 입장 코드를 만들지 않았어요.")
    if not hmac.compare_digest(code, join_code):
        fail(403, "입장 코드가 맞지 않아요. 선생님께 다시 확인해 주세요.")
    if db.execute("SELECT 1 FROM students WHERE nickname = ?", (nickname,)).fetchone():
        fail(409, "이미 다른 친구가 쓰는 닉네임이에요. 다른 닉네임을 골라 주세요.")

    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO students(nickname, token_hash) VALUES(?, ?)", (nickname, hash_token(token)))
    return {"token": token, "nickname": nickname}


@app.get("/api/me")
def me(student: Student):
    return {"nickname": student["nickname"]}


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
    sql = """SELECT r.id, m.date, m.name AS menu, s.nickname, r.stars, r.comment,
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


# ---------------------------------------------------------------- 선생님: 입장 코드와 학생

CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 헷갈리는 0/O, 1/I/L 은 빼요


@app.get("/api/teacher/join-code")
def get_join_code(_: Teacher, db: Db):
    return {"code": get_setting(db, "join_code")}


@app.post("/api/teacher/join-code")
def new_join_code(_: Teacher, db: Db):
    code = "".join(secrets.choice(CODE_CHARS) for _ in range(6))
    set_setting(db, "join_code", code)
    return {"code": code}


@app.get("/api/teacher/students")
def students(_: Teacher, db: Db):
    rows = db.execute("SELECT id, nickname, created_at FROM students ORDER BY nickname").fetchall()
    return {"students": [dict(r) for r in rows]}


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
