"""SQLite 데이터베이스 (파일 하나: backend/data/lunch.db)."""
import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 학생 계정. 친구들에게는 닉네임만 보여요. 진짜 이름(real_name)은 선생님 화면에만 나와요.
-- 예전(입장 코드) 방식으로 들어온 학생은 login_id 가 비어 있고, 다음에 들어올 때 계정을 만들어요.
CREATE TABLE IF NOT EXISTS students (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    nickname       TEXT NOT NULL UNIQUE,
    login_id       TEXT UNIQUE,
    password_hash  TEXT,
    real_name      TEXT,
    grade          INTEGER,
    class_no       INTEGER,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 학생 로그인 기록. 기기마다 하나씩, 로그아웃하면 지워요.
CREATE TABLE IF NOT EXISTS student_sessions (
    token_hash  TEXT PRIMARY KEY,
    student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 선생님 로그인 기록
CREATE TABLE IF NOT EXISTS teacher_sessions (
    token_hash  TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS menus (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    date    TEXT NOT NULL,              -- YYYY-MM-DD
    name    TEXT NOT NULL,
    ord     INTEGER NOT NULL DEFAULT 0,
    source  TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('neis', 'manual'))
);
CREATE INDEX IF NOT EXISTS menus_date ON menus(date);

-- 별점 + 한 줄 후기. 한 메뉴에 한 사람당 하나.
CREATE TABLE IF NOT EXISTS reviews (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    menu_id       INTEGER NOT NULL REFERENCES menus(id) ON DELETE CASCADE,
    student_id    INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    stars         INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 5),
    comment       TEXT NOT NULL DEFAULT '' CHECK (length(comment) <= 50),
    report_count  INTEGER NOT NULL DEFAULT 0,
    hidden        INTEGER NOT NULL DEFAULT 0,
    hidden_by     TEXT,                 -- 'reports' | 'teacher'
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (menu_id, student_id)
);

-- 나이스에서 이미 가져온 날짜. 한 번 가져온 날은 선생님이 고친 대로 두고 다시 덮지 않아요.
CREATE TABLE IF NOT EXISTS neis_days (
    date         TEXT PRIMARY KEY,
    imported_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 후기 좋아요: 한 사람이 한 후기에 한 번 (다시 누르면 취소)
CREATE TABLE IF NOT EXISTS review_likes (
    review_id   INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (review_id, student_id)
);

-- 나이스 학사일정 (행사, 방학, 재량휴업일)
CREATE TABLE IF NOT EXISTS school_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    date     TEXT NOT NULL,
    name     TEXT NOT NULL,
    no_school INTEGER NOT NULL DEFAULT 0   -- 휴업일·공휴일·방학이면 1
);
CREATE INDEX IF NOT EXISTS school_events_date ON school_events(date);

-- 신고는 한 사람이 한 후기에 한 번만
CREATE TABLE IF NOT EXISTS reports (
    review_id   INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (review_id, student_id)
);
"""


def db_path() -> Path:
    default = Path(__file__).resolve().parent.parent / "data" / "lunch.db"
    return Path(os.environ.get("DB_PATH", default))


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        migrate_students(conn)
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def migrate_students(conn: sqlite3.Connection) -> None:
    """입장 코드 시절 students 표(token_hash 칸)를 계정 표로 바꿔요.
    학생 번호(id)는 그대로라서 별점·후기·좋아요·신고가 모두 남아요.
    바꾸기 전에 lunch.db 를 lunch-before-accounts.db 로 한 벌 복사해 둬요."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(students)")}
    if "token_hash" not in cols:
        return
    backup = db_path().with_name("lunch-before-accounts.db")
    if not backup.exists():
        dest = sqlite3.connect(backup)
        conn.backup(dest)
        dest.close()
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")  # 표를 바꾸는 동안 후기가 같이 지워지지 않게
    try:
        conn.executescript("""
            BEGIN;
            CREATE TABLE students_new (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                nickname       TEXT NOT NULL UNIQUE,
                login_id       TEXT UNIQUE,
                password_hash  TEXT,
                real_name      TEXT,
                grade          INTEGER,
                class_no       INTEGER,
                created_at     TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO students_new(id, nickname, created_at) SELECT id, nickname, created_at FROM students;
            CREATE TABLE IF NOT EXISTS student_sessions (
                token_hash  TEXT PRIMARY KEY,
                student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT OR IGNORE INTO student_sessions(token_hash, student_id) SELECT token_hash, id FROM students;
            DROP TABLE students;
            ALTER TABLE students_new RENAME TO students;
            COMMIT;
        """)
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(f"학생 표를 바꾸다가 연결이 끊긴 기록이 {len(broken)}개 있어요. {backup} 로 되돌려 주세요.")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
