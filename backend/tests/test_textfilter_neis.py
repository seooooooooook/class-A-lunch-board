from app.neis import build_meal_url, clean_menu_name, extract_lunches, parse_menu_names
from app.textfilter import check_text


def test_bad_words_and_bypass():
    assert check_text("오늘 국 존나 짰음") == "badword"
    assert check_text("씨 1 발") == "badword"
    assert check_text("급식 아줌마 최고") == "badword"
    assert check_text("민수 바보 아님", ["민수"]) == "badword"


def test_personal_info():
    assert check_text("010-1234-5678 연락해") == "personal"
    assert check_text("www.example.com 가봐") == "personal"
    assert check_text("101동 1203호 놀러와") == "personal"


def test_normal_reviews_pass():
    for t in ["줄서라도 먹고 싶은 맛", "국이 조금 짰어요", "호박죽 어때? 맛있었어",
              "토마토 나물 최고", "급식으로 2그릇 먹음", "할매순대국 같은 맛"]:
        assert check_text(t) is None, t


def test_clean_menu_names():
    assert clean_menu_name("떡볶이(1.5.6)") == "떡볶이"
    assert clean_menu_name("쌀밥 5.6.") == "쌀밥"
    assert clean_menu_name("배추김치9.") == "배추김치"
    assert clean_menu_name("*우유 (2)") == "우유"
    assert clean_menu_name("3색나물(5.6)") == "3색나물"
    assert clean_menu_name("닭갈비(완)(5.6.13.15)") == "닭갈비(완)"
    assert parse_menu_names("현미밥<br/>떡볶이(1.5.6)<br/>김치(9)<br/>") == ["현미밥", "떡볶이", "김치"]


def test_extract_lunches():
    data = {"mealServiceDietInfo": [
        {"head": [{"list_total_count": 3}, {"RESULT": {"CODE": "INFO-000", "MESSAGE": "정상"}}]},
        {"row": [
            {"MLSV_YMD": "20261117", "MMEAL_SC_CODE": "1", "DDISH_NM": "토스트"},
            {"MLSV_YMD": "20261117", "MMEAL_SC_CODE": "2", "DDISH_NM": "쌀밥<br/>된장국(5.6)"},
            {"MLSV_YMD": "20261116", "MMEAL_SC_CODE": "2", "DDISH_NM": "카레라이스(1.2)"},
        ]},
    ]}
    assert extract_lunches(data) == {"status": "ok", "days": {"2026-11-16": ["카레라이스"], "2026-11-17": ["쌀밥", "된장국"]}}
    assert extract_lunches({"RESULT": {"CODE": "INFO-200"}}) == {"status": "ok", "days": {}}
    assert extract_lunches({"RESULT": {"CODE": "ERROR-290", "MESSAGE": "x"}})["code"] == "ERROR-290"


def test_build_url():
    url = build_meal_url("K", "J10", "7591034", "2026-10-19", "2026-11-17")
    assert url.startswith("https://open.neis.go.kr/hub/mealServiceDietInfo?")
    assert "MLSV_FROM_YMD=20261019" in url and "MLSV_TO_YMD=20261117" in url and "pSize=100" in url
