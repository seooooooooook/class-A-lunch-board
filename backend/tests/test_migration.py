"""입장 코드 시절 데이터베이스를 계정 방식으로 바꿔도 별점·후기·좋아요가 남는지 확인해요."""
import hashlib
import sqlite3
from datetime import datetime

from fastapi.testclient import TestClient

from app.main import KST

OLD_SCHEMA = """
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE students (
    id INTEGER PRIMARY KEY AUTOINCREMENT, nickname TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE menus (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, name TEXT NOT NULL,
    ord INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'manual');
CREATE TABLE reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    menu_id INTEGER NOT NULL REFERENCES menus(id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    stars INTEGER NOT NULL, comment TEXT NOT NULL DEFAULT '', report_count INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0, hidden_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (menu_id, student_id));
CREATE TABLE review_likes (
    review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')), PRIMARY KEY (review_id, student_id));
"""


def sha(t):
    return hashlib.sha256(t.encode()).hexdigest()


def test_old_database_keeps_reviews(tmp_path, monkeypatch):
    path = tmp_path / "lunch.db"
    today = datetime.now(KST).strftime("%Y-%m-%d")
    old = sqlite3.connect(path)
    old.executescript(OLD_SCHEMA)
    old.execute("INSERT INTO settings VALUES('join_code', 'KIMCHI')")
    old.execute("INSERT INTO students(id, nickname, token_hash) VALUES(1, '떡볶이왕', ?), (2, '김치왕', ?)", (sha("tok1"), sha("tok2")))
    old.execute("INSERT INTO menus(id, date, name) VALUES(1, ?, '카레')", (today,))
    old.execute("INSERT INTO reviews(id, menu_id, student_id, stars, comment) VALUES(1, 1, 1, 5, '맛있어요')")
    old.execute("INSERT INTO review_likes(review_id, student_id) VALUES(1, 2)")
    old.commit()
    old.close()

    monkeypatch.setenv("DB_PATH", str(path))
    from app.main import app
    with TestClient(app) as client:
        kid1, kid2 = {"Authorization": "Bearer tok1"}, {"Authorization": "Bearer tok2"}
        # 예전 기기는 로그인된 채로, 계정을 먼저 만들라고 안내받아요
        me = client.get("/api/me", headers=kid1).json()
        assert me == {"nickname": "떡볶이왕", "class_name": None, "login_id": None, "needs_account": True}
        assert client.get(f"/api/days/{today}", headers=kid1).status_code == 403
        res = client.post("/api/me/account", headers=kid1,
                          json={"class_name": "5-1", "real_name": "김철수", "login_id": "tteok", "password": "1234"})
        assert res.status_code == 200 and res.json()["needs_account"] is False
        assert client.post("/api/me/account", headers=kid1,
                           json={"class_name": "5-1", "real_name": "김철수", "login_id": "tteok", "password": "1234"}).status_code == 409
        client.post("/api/me/account", headers=kid2,
                    json={"class_name": "5-1", "real_name": "이영희", "login_id": "kimchi", "password": "1234"})

        menu = client.get(f"/api/days/{today}", headers=kid2).json()["menus"][0]
        assert menu["avg"] == 5 and menu["reviews"][0]["comment"] == "맛있어요"
        assert menu["reviews"][0]["likes"] == 1 and menu["reviews"][0]["liked"] is True
        # 새 비밀번호로 다른 기기에서도 로그인
        assert client.post("/api/login", json={"login_id": "tteok", "password": "1234"}).status_code == 200

    assert (tmp_path / "lunch-before-accounts.db").exists()
    # 두 번째로 켜도 아무 일 없음
    with TestClient(app) as client:
        assert client.get("/api/me", headers={"Authorization": "Bearer tok1"}).json()["login_id"] == "tteok"
