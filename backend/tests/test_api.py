from datetime import datetime, timedelta

import httpx
import pytest

import asyncio

from app import main, sync
from app.main import KST

TODAY = datetime.now(KST).strftime("%Y-%m-%d")
YESTERDAY = (datetime.now(KST) - timedelta(days=1)).strftime("%Y-%m-%d")


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def teacher(client):
    res = client.post("/api/teacher/login", json={"password": "teacher-pw"})
    assert res.status_code == 200
    return auth(res.json()["token"])


@pytest.fixture()
def setup(client, teacher):
    """입장 코드 + 오늘 메뉴 2개 + 어제 메뉴 1개 + 학생 4명"""
    code = client.post("/api/teacher/join-code", headers=teacher).json()["code"]
    today = client.put(f"/api/teacher/menus/{TODAY}", headers=teacher,
                       json={"menus": [{"name": "떡볶이", "source": "neis"}, {"name": "김치"}]}).json()["menus"]
    old = client.put(f"/api/teacher/menus/{YESTERDAY}", headers=teacher, json={"menus": [{"name": "카레"}]}).json()["menus"]
    students = {}
    for nick in ["떡볶이왕", "김치왕", "라면왕", "만두왕"]:
        res = client.post("/api/join", json={"code": code.lower(), "nickname": nick})
        assert res.status_code == 200, res.text
        students[nick] = auth(res.json()["token"])
    return {"code": code, "menu": today[0]["id"], "menu2": today[1]["id"], "old": old[0]["id"], "s": students}


def test_teacher_login(client):
    assert client.post("/api/teacher/login", json={"password": "nope"}).status_code == 403
    assert client.get("/api/teacher/students").status_code == 401


def test_join_rules(client, teacher, setup):
    assert client.post("/api/join", json={"code": "WRONG1", "nickname": "새친구"}).status_code == 403
    assert client.post("/api/join", json={"code": setup["code"], "nickname": "김치왕"}).status_code == 409
    assert client.post("/api/join", json={"code": setup["code"], "nickname": "바보"}).status_code == 400
    assert client.post("/api/join", json={"code": setup["code"], "nickname": "가" * 11}).status_code == 400


def test_students_cannot_use_teacher_api(client, setup):
    s = setup["s"]["떡볶이왕"]
    assert client.get("/api/teacher/join-code", headers=s).status_code == 401
    assert client.put(f"/api/teacher/menus/{TODAY}", headers=s, json={"menus": []}).status_code == 401


def test_rating_average_and_update(client, setup):
    s, menu = setup["s"], setup["menu"]
    assert client.put(f"/api/menus/{menu}/review", headers=s["떡볶이왕"], json={"stars": 5}).status_code == 200
    assert client.put(f"/api/menus/{menu}/review", headers=s["김치왕"], json={"stars": 4, "comment": "맛있어요"}).status_code == 200
    # 다시 누르면 새로 생기지 않고 점수만 바뀜
    client.put(f"/api/menus/{menu}/review", headers=s["떡볶이왕"], json={"stars": 3})
    day = client.get(f"/api/days/{TODAY}", headers=s["라면왕"]).json()
    m = next(x for x in day["menus"] if x["id"] == menu)
    assert m["count"] == 2 and m["avg"] == 3.5
    assert [r["comment"] for r in m["reviews"]] == ["맛있어요"]  # 별점만 준 건 후기 목록에 없음
    assert m["mine"] is None
    other = next(x for x in day["menus"] if x["id"] == setup["menu2"])
    assert other["avg"] is None


def test_review_validation(client, setup):
    s, menu = setup["s"]["떡볶이왕"], setup["menu"]
    assert client.put(f"/api/menus/{menu}/review", headers=s, json={"stars": 6}).status_code == 422
    assert client.put(f"/api/menus/{menu}/review", headers=s, json={"stars": 0}).status_code == 422
    assert client.put(f"/api/menus/{menu}/review", headers=s, json={"stars": 5, "comment": "가" * 51}).status_code == 400
    res = client.put(f"/api/menus/{menu}/review", headers=s, json={"stars": 5, "comment": "존나 맛있어"})
    assert res.status_code == 400 and "고운 말" in res.json()["detail"]
    assert client.put(f"/api/menus/{menu}/review", headers=s, json={"stars": 5, "comment": "010-1234-5678"}).status_code == 400
    assert client.put(f"/api/menus/{setup['old']}/review", headers=s, json={"stars": 5}).status_code == 403  # 지난 날
    assert client.put(f"/api/menus/{menu}/review", json={"stars": 5}).status_code == 401  # 입장 안 함


def test_teacher_banned_word_applies(client, teacher, setup):
    client.post("/api/teacher/banned-words", headers=teacher, json={"word": "놀림말"})
    res = client.put(f"/api/menus/{setup['menu']}/review", headers=setup["s"]["떡볶이왕"], json={"stars": 1, "comment": "놀 림 말"})
    assert res.status_code == 400


def test_three_reports_hide(client, teacher, setup):
    s, menu = setup["s"], setup["menu"]
    client.put(f"/api/menus/{menu}/review", headers=s["떡볶이왕"], json={"stars": 5, "comment": "줄서라도 먹고 싶은 맛"})
    rid = client.get(f"/api/days/{TODAY}", headers=s["김치왕"]).json()["menus"][0]["reviews"][0]["id"]

    assert client.post(f"/api/reviews/{rid}/report", headers=s["떡볶이왕"]).status_code == 400  # 내 글
    assert client.post(f"/api/reviews/{rid}/report", headers=s["김치왕"]).json()["hidden"] is False
    assert client.post(f"/api/reviews/{rid}/report", headers=s["김치왕"]).status_code == 409  # 두 번
    client.post(f"/api/reviews/{rid}/report", headers=s["라면왕"])
    assert client.post(f"/api/reviews/{rid}/report", headers=s["만두왕"]).json()["hidden"] is True

    # 다른 친구에게는 안 보이고, 쓴 사람은 숨겨졌다는 걸 알아요
    m = client.get(f"/api/days/{TODAY}", headers=s["김치왕"]).json()["menus"][0]
    assert m["reviews"] == [] and m["count"] == 0
    mine = client.get(f"/api/days/{TODAY}", headers=s["떡볶이왕"]).json()["menus"][0]["mine"]
    assert mine["hidden"] is True
    # 학생이 다시 저장해도 숨김은 안 풀려요
    client.put(f"/api/menus/{menu}/review", headers=s["떡볶이왕"], json={"stars": 5, "comment": "다시 씀"})
    assert client.get(f"/api/days/{TODAY}", headers=s["김치왕"]).json()["menus"][0]["reviews"] == []

    flagged = client.get("/api/teacher/reviews?mode=flagged", headers=teacher).json()["reviews"]
    assert flagged[0]["hidden_by"] == "reports" and flagged[0]["report_count"] == 3
    client.patch(f"/api/teacher/reviews/{rid}", headers=teacher, json={"hidden": False})
    assert len(client.get(f"/api/days/{TODAY}", headers=s["김치왕"]).json()["menus"][0]["reviews"]) == 1


def test_delete_own_review(client, setup):
    s, menu = setup["s"]["떡볶이왕"], setup["menu"]
    client.put(f"/api/menus/{menu}/review", headers=s, json={"stars": 5})
    assert client.delete(f"/api/menus/{menu}/review", headers=s).status_code == 200
    assert client.get(f"/api/days/{TODAY}", headers=s).json()["menus"][0]["mine"] is None


def test_menu_edit_keeps_reviews(client, teacher, setup):
    s = setup["s"]["떡볶이왕"]
    client.put(f"/api/menus/{setup['menu']}/review", headers=s, json={"stars": 5})
    menus = client.get(f"/api/teacher/menus?date={TODAY}", headers=teacher).json()["menus"]
    # 이름 바꾸고 순서 바꾸기
    body = {"menus": [{"id": menus[1]["id"], "name": "배추김치"}, {"id": menus[0]["id"], "name": "국물떡볶이"}]}
    saved = client.put(f"/api/teacher/menus/{TODAY}", headers=teacher, json=body).json()["menus"]
    assert [m["name"] for m in saved] == ["배추김치", "국물떡볶이"]
    assert saved[1]["review_count"] == 1
    # 다른 날 메뉴 id 를 섞으면 거절
    bad = {"menus": [{"id": setup["old"], "name": "카레"}]}
    assert client.put(f"/api/teacher/menus/{TODAY}", headers=teacher, json=bad).status_code == 400


def test_rename_student(client, teacher, setup):
    students = client.get("/api/teacher/students", headers=teacher).json()["students"]
    sid = next(x["id"] for x in students if x["nickname"] == "떡볶이왕")
    client.put(f"/api/menus/{setup['menu']}/review", headers=setup["s"]["떡볶이왕"], json={"stars": 5, "comment": "맛있다"})
    assert client.patch(f"/api/teacher/students/{sid}", headers=teacher, json={"nickname": "김치왕"}).status_code == 409
    assert client.patch(f"/api/teacher/students/{sid}", headers=teacher, json={"nickname": "떡순이"}).status_code == 200
    r = client.get(f"/api/days/{TODAY}", headers=setup["s"]["김치왕"]).json()["menus"][0]["reviews"][0]
    assert r["nickname"] == "떡순이"


REAL_ASYNC_CLIENT = httpx.AsyncClient


def _fake_neis(monkeypatch, payload, schedule=None):
    seen = {}

    def handler(request):
        if "SchoolSchedule" in request.url.path:
            seen["schedule_url"] = str(request.url)
            return httpx.Response(200, json=schedule or {"RESULT": {"CODE": "INFO-200"}})
        seen["url"] = str(request.url)
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(sync.httpx, "AsyncClient", lambda **kw: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw))
    return seen


def _rows(*days):
    return {"mealServiceDietInfo": [
        {"head": [{"list_total_count": len(days)}, {"RESULT": {"CODE": "INFO-000"}}]},
        {"row": [{"MLSV_YMD": d.replace("-", ""), "MMEAL_SC_CODE": "2", "DDISH_NM": menu} for d, menu in days]},
    ]}


NOW = datetime(2026, 11, 17, 9, 0, tzinfo=KST)


def test_sync_fills_last_30_days(client, teacher, monkeypatch):
    seen = _fake_neis(monkeypatch, _rows(("2026-11-16", "카레(1.2)<br/>김치"), ("2026-11-17", "통밀밥<br/>닭갈비(5.6)")))
    status = asyncio.run(sync.run_sync(NOW))
    assert status["ok"] and status["added"] == ["2026-11-16", "2026-11-17"]
    assert "MLSV_FROM_YMD=20261019" in seen["url"] and "MLSV_TO_YMD=20261117" in seen["url"]
    assert "SD_SCHUL_CODE=7010000" in seen["url"]
    menus = client.get("/api/teacher/menus?date=2026-11-17", headers=teacher).json()["menus"]
    assert [(m["name"], m["source"]) for m in menus] == [("통밀밥", "neis"), ("닭갈비", "neis")]
    st = client.get("/api/teacher/neis-status", headers=teacher).json()
    assert st["last"]["ok"] and st["hour"] == 9 and st["days"] == 30


def test_sync_keeps_teacher_edits(client, teacher, monkeypatch):
    # 선생님이 미리 넣은 날은 건드리지 않음
    client.put("/api/teacher/menus/2026-11-16", headers=teacher, json={"menus": [{"name": "직접 넣은 메뉴"}]})
    _fake_neis(monkeypatch, _rows(("2026-11-16", "카레"), ("2026-11-17", "통밀밥<br/>요구르트")))
    asyncio.run(sync.run_sync(NOW))
    assert [m["name"] for m in client.get("/api/teacher/menus?date=2026-11-16", headers=teacher).json()["menus"]] == ["직접 넣은 메뉴"]
    # 가져온 뒤 선생님이 고치면, 다음 날 다시 불러와도 그대로
    menus = client.get("/api/teacher/menus?date=2026-11-17", headers=teacher).json()["menus"]
    client.put("/api/teacher/menus/2026-11-17", headers=teacher,
               json={"menus": [{"id": menus[0]["id"], "name": "통밀밥"}, {"id": menus[1]["id"], "name": "요구르트(딸기)"}]})
    status = asyncio.run(sync.run_sync(NOW + timedelta(days=1)))
    assert status["added"] == []
    assert [m["name"] for m in client.get("/api/teacher/menus?date=2026-11-17", headers=teacher).json()["menus"]] == ["통밀밥", "요구르트(딸기)"]


def test_sync_bad_key_records_error(client, teacher, monkeypatch):
    _fake_neis(monkeypatch, {"RESULT": {"CODE": "ERROR-290", "MESSAGE": "인증키가 유효하지 않습니다."}})
    status = asyncio.run(sync.run_sync(NOW))
    assert not status["ok"] and "인증키" in status["message"]
    assert not client.get("/api/teacher/neis-status", headers=teacher).json()["last"]["ok"]


def test_next_run_time():
    assert sync.next_run_after(datetime(2026, 11, 17, 8, 59, tzinfo=KST)) == datetime(2026, 11, 17, 9, 0, tzinfo=KST)
    assert sync.next_run_after(datetime(2026, 11, 17, 9, 0, tzinfo=KST)) == datetime(2026, 11, 18, 9, 0, tzinfo=KST)
    assert sync.next_run_after(datetime(2026, 11, 17, 23, 0, tzinfo=KST)) == datetime(2026, 11, 18, 9, 0, tzinfo=KST)


def test_due_rules(client, monkeypatch):
    morning = datetime(2026, 11, 17, 8, 0, tzinfo=KST)
    nine = datetime(2026, 11, 17, 9, 1, tzinfo=KST)
    assert sync.due(morning, None, 0)          # 처음 켰을 때는 9시 전이어도 한 번
    assert sync.due(nine, None, 0)
    _fake_neis(monkeypatch, _rows(("2026-11-17", "통밀밥")))
    asyncio.run(sync.run_sync(nine))
    assert not sync.due(nine + timedelta(hours=3), None, 1)   # 오늘 성공했으면 끝
    tomorrow_8 = datetime(2026, 11, 18, 8, 0, tzinfo=KST)
    assert not sync.due(tomorrow_8, None, 0)  # 다음 날 9시 전에는 안 함
    assert sync.due(datetime(2026, 11, 18, 9, 0, tzinfo=KST), None, 0)
    # 실패하면 30분 뒤에, 최대 3번 더
    _fake_neis(monkeypatch, {"RESULT": {"CODE": "ERROR-500"}})
    t = datetime(2026, 11, 18, 9, 0, tzinfo=KST)
    asyncio.run(sync.run_sync(t))
    assert not sync.due(t + timedelta(minutes=10), t, 1)
    assert sync.due(t + timedelta(minutes=31), t, 1)
    assert not sync.due(t + timedelta(hours=5), t, 4)


def test_schedule_events_and_holiday(client, teacher, setup, monkeypatch):
    schedule = {"SchoolSchedule": [
        {"head": [{"list_total_count": 4}, {"RESULT": {"CODE": "INFO-000"}}]},
        {"row": [
            {"AA_YMD": "20261117", "EVENT_NM": "학부모 공개수업", "SBTR_DD_SC_NM": "해당없음", "FIVE_GRADE_EVENT_YN": "Y", "SIX_GRADE_EVENT_YN": "Y"},
            {"AA_YMD": "20261117", "EVENT_NM": "1학년 현장체험학습", "SBTR_DD_SC_NM": "해당없음", "FIVE_GRADE_EVENT_YN": "N", "SIX_GRADE_EVENT_YN": "N"},
            {"AA_YMD": "20261121", "EVENT_NM": "토요휴업일", "SBTR_DD_SC_NM": "휴업일"},
            {"AA_YMD": "20261120", "EVENT_NM": "재량휴업일", "SBTR_DD_SC_NM": "휴업일", "FIVE_GRADE_EVENT_YN": "Y", "SIX_GRADE_EVENT_YN": "Y"},
        ]},
    ]}
    seen = _fake_neis(monkeypatch, _rows(("2026-11-17", "통밀밥")), schedule)
    status = asyncio.run(sync.run_sync(NOW))
    assert status["ok"] and status["events"] == 2
    assert "AA_FROM_YMD=20261019" in seen["schedule_url"] and "AA_TO_YMD=20261217" in seen["schedule_url"]
    s = setup["s"]["떡볶이왕"]
    assert client.get("/api/days/2026-11-17", headers=s).json()["events"] == [{"name": "학부모 공개수업", "no_school": False}]
    assert client.get("/api/days/2026-11-20", headers=s).json()["events"] == [{"name": "재량휴업일", "no_school": True}]
    assert client.get("/api/days/2026-11-21", headers=s).json()["events"] == []
    # 다음 날 다시 받으면 바뀐 일정으로 바뀜
    _fake_neis(monkeypatch, {"RESULT": {"CODE": "INFO-200"}}, {"RESULT": {"CODE": "INFO-200"}})
    asyncio.run(sync.run_sync(NOW))
    assert client.get("/api/days/2026-11-20", headers=s).json()["events"] == []


def test_schedule_failure_keeps_meals(client, teacher, monkeypatch):
    _fake_neis(monkeypatch, _rows(("2026-11-17", "통밀밥")), {"RESULT": {"CODE": "ERROR-500"}})
    status = asyncio.run(sync.run_sync(NOW))
    assert status["ok"] and "학사일정은 불러오지 못했어요" in status["message"]


def test_likes(client, setup):
    s, menu = setup["s"], setup["menu"]
    client.put(f"/api/menus/{menu}/review", headers=s["떡볶이왕"], json={"stars": 5, "comment": "맛있어요"})
    rid = client.get(f"/api/days/{TODAY}", headers=s["김치왕"]).json()["menus"][0]["reviews"][0]["id"]
    assert client.put(f"/api/reviews/{rid}/like", headers=s["떡볶이왕"]).status_code == 400  # 내 후기
    assert client.put(f"/api/reviews/{rid}/like", headers=s["김치왕"]).json() == {"likes": 1, "liked": True}
    assert client.put(f"/api/reviews/{rid}/like", headers=s["김치왕"]).json()["likes"] == 1  # 두 번 눌러도 1
    client.put(f"/api/reviews/{rid}/like", headers=s["라면왕"])
    r = client.get(f"/api/days/{TODAY}", headers=s["김치왕"]).json()["menus"][0]["reviews"][0]
    assert r["likes"] == 2 and r["liked"] is True
    assert client.get(f"/api/days/{TODAY}", headers=s["만두왕"]).json()["menus"][0]["reviews"][0]["liked"] is False
    assert client.delete(f"/api/reviews/{rid}/like", headers=s["김치왕"]).json() == {"likes": 1, "liked": False}
    assert client.put(f"/api/reviews/{rid}/like").status_code == 401


def test_cannot_like_hidden(client, teacher, setup):
    s, menu = setup["s"], setup["menu"]
    client.put(f"/api/menus/{menu}/review", headers=s["떡볶이왕"], json={"stars": 5, "comment": "맛있어요"})
    rid = client.get(f"/api/days/{TODAY}", headers=s["김치왕"]).json()["menus"][0]["reviews"][0]["id"]
    client.patch(f"/api/teacher/reviews/{rid}", headers=teacher, json={"hidden": True})
    assert client.put(f"/api/reviews/{rid}/like", headers=s["김치왕"]).status_code == 404


def test_teacher_cannot_call_neis_directly(client, teacher):
    assert client.post(f"/api/teacher/neis?date={TODAY}", headers=teacher).status_code in (404, 405)


def test_pages_served_noindex(client):
    res = client.get("/")
    assert res.status_code == 200 and "오늘의 급식 평가판" in res.text
    assert res.headers["x-robots-tag"] == "noindex, nofollow"
    assert client.get("/admin.html").status_code == 200
