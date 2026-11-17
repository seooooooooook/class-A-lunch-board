"""나이스(NEIS) 급식식단정보 API 도우미.
문서: https://open.neis.go.kr → 급식식단정보 (mealServiceDietInfo)"""
import re
from urllib.parse import urlencode

DEFAULT_API_BASE = "https://open.neis.go.kr/hub"
LUNCH_CODE = "2"  # MMEAL_SC_CODE: 1 조식, 2 중식, 3 석식

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def build_meal_url(api_key: str, office_code: str, school_code: str, date_from: str, date_to: str,
                   api_base: str | None = None) -> str:
    """date_from ~ date_to (YYYY-MM-DD) 사이 급식을 한 번에 달라고 하는 주소."""
    if not (_DATE_RE.match(date_from or "") and _DATE_RE.match(date_to or "")):
        raise ValueError("날짜는 YYYY-MM-DD 모양이어야 해요.")
    params = {
        "KEY": api_key,
        "Type": "json",
        "pIndex": "1",
        "pSize": "100",  # 30일 × 하루 최대 3끼(조·중·석식) = 90줄
        "ATPT_OFCDC_SC_CODE": office_code,
        "SD_SCHUL_CODE": school_code,
        "MLSV_FROM_YMD": date_from.replace("-", ""),
        "MLSV_TO_YMD": date_to.replace("-", ""),
    }
    return f"{(api_base or DEFAULT_API_BASE).rstrip('/')}/mealServiceDietInfo?{urlencode(params)}"


def build_schedule_url(api_key: str, office_code: str, school_code: str, date_from: str, date_to: str,
                       api_base: str | None = None) -> str:
    """학사일정(SchoolSchedule) 주소."""
    if not (_DATE_RE.match(date_from or "") and _DATE_RE.match(date_to or "")):
        raise ValueError("날짜는 YYYY-MM-DD 모양이어야 해요.")
    params = {
        "KEY": api_key,
        "Type": "json",
        "pIndex": "1",
        "pSize": "200",
        "ATPT_OFCDC_SC_CODE": office_code,
        "SD_SCHUL_CODE": school_code,
        "AA_FROM_YMD": date_from.replace("-", ""),
        "AA_TO_YMD": date_to.replace("-", ""),
    }
    return f"{(api_base or DEFAULT_API_BASE).rstrip('/')}/SchoolSchedule?{urlencode(params)}"


NO_SCHOOL_KINDS = {"휴업일", "공휴일"}
SKIP_EVENTS = {"토요휴업일"}  # 주말은 화면이 이미 알려 줘요


def _grade_event(row: dict) -> bool:
    """5·6학년에 해당하는 행사인지. 학년 칸이 없으면 학교 전체 행사로 봐요."""
    keys = ("FIVE_GRADE_EVENT_YN", "SIX_GRADE_EVENT_YN")
    if not any(k in row for k in keys):
        return True
    return any(row.get(k) == "Y" for k in keys)


def extract_events(data: dict) -> dict:
    """{'status': 'ok', 'events': [{'date', 'name', 'no_school'}]} 또는 error"""
    if isinstance(data, dict) and "RESULT" in data:
        code = data["RESULT"].get("CODE")
        if code == "INFO-200":
            return {"status": "ok", "events": []}
        return {"status": "error", "code": code, "message": data["RESULT"].get("MESSAGE", "")}
    blocks = data.get("SchoolSchedule") if isinstance(data, dict) else None
    if not isinstance(blocks, list):
        return {"status": "error", "code": "UNKNOWN", "message": "나이스 응답 모양이 예상과 달라요."}
    head = next((b["head"] for b in blocks if "head" in b), [])
    result = next((h["RESULT"] for h in head if "RESULT" in h), None)
    if result and result.get("CODE") not in ("INFO-000", "INFO-200"):
        return {"status": "error", "code": result.get("CODE"), "message": result.get("MESSAGE", "")}
    events = []
    for row in next((b["row"] for b in blocks if "row" in b), []):
        ymd = str(row.get("AA_YMD", ""))
        name = " ".join(str(row.get("EVENT_NM", "")).split())[:40]
        if not re.fullmatch(r"\d{8}", ymd) or not name or name in SKIP_EVENTS:
            continue
        no_school = row.get("SBTR_DD_SC_NM") in NO_SCHOOL_KINDS or "방학" in name
        if not (no_school or _grade_event(row)):
            continue
        events.append({"date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}", "name": name, "no_school": no_school})
    return {"status": "ok", "events": events}


def clean_menu_name(raw: str) -> str:
    """'떡볶이(1.5.6)' → '떡볶이', '쌀밥 5.6.' → '쌀밥', '*우유 (2)' → '우유'"""
    s = re.sub(r"<[^>]*>", " ", raw or "")
    s = re.sub(r"\(\s*[\d.,\s]+\s*\)", "", s)          # (1.5.6)
    s = re.sub(r"\s+[\d.,]+\.?\s*$", "", s)              # 끝에 붙은 ' 1.2.5.'
    s = re.sub(r"(?<=[가-힣)\]])[\d.,]*\d\.\s*$", "", s)  # '김치9.'
    s = re.sub(r"^[*#&^@~\s]+|[*#&^@~\s]+$", "", s)      # 앞뒤 표시 기호
    return re.sub(r"\s{2,}", " ", s).strip()


def parse_menu_names(ddish_nm: str) -> list[str]:
    parts = re.split(r"<br\s*/?>|\n", ddish_nm or "", flags=re.IGNORECASE)
    return [name for name in (clean_menu_name(p) for p in parts) if name]


def extract_lunches(data: dict) -> dict:
    """나이스 응답에서 날짜별 중식 메뉴를 꺼내요.
    {'status': 'ok', 'days': {'2026-11-17': ['통밀밥', ...], ...}}
    또는 {'status': 'error', 'code': ..., 'message': ...}
    급식이 하루도 없으면(방학 등) status 는 'ok', days 는 빈 dict 예요."""
    if isinstance(data, dict) and "RESULT" in data:
        code = data["RESULT"].get("CODE")
        if code == "INFO-200":
            return {"status": "ok", "days": {}}
        return {"status": "error", "code": code, "message": data["RESULT"].get("MESSAGE", "")}

    blocks = data.get("mealServiceDietInfo") if isinstance(data, dict) else None
    if not isinstance(blocks, list):
        return {"status": "error", "code": "UNKNOWN", "message": "나이스 응답 모양이 예상과 달라요."}

    head = next((b["head"] for b in blocks if "head" in b), [])
    result = next((h["RESULT"] for h in head if "RESULT" in h), None)
    if result and result.get("CODE") != "INFO-000":
        if result.get("CODE") == "INFO-200":
            return {"status": "ok", "days": {}}
        return {"status": "error", "code": result.get("CODE"), "message": result.get("MESSAGE", "")}

    days: dict[str, list[str]] = {}
    for row in next((b["row"] for b in blocks if "row" in b), []):
        ymd = str(row.get("MLSV_YMD", ""))
        if str(row.get("MMEAL_SC_CODE")) != LUNCH_CODE or not re.fullmatch(r"\d{8}", ymd):
            continue
        names = parse_menu_names(row.get("DDISH_NM", ""))
        if names:
            days[f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"] = names
    return {"status": "ok", "days": dict(sorted(days.items()))}


_ERRORS = {
    "ERROR-290": "나이스 인증키가 올바르지 않아요. backend/.env 의 NEIS_API_KEY 를 확인해 주세요.",
    "ERROR-300": "교육청 코드나 학교 코드가 비어 있어요. backend/.env 를 확인해 주세요.",
    "ERROR-310": "나이스 서비스 이름이 잘못되었어요.",
    "ERROR-336": "한 번에 너무 많은 데이터를 요청했어요.",
    "ERROR-337": "오늘 나이스 API 사용량을 다 썼어요. 내일 다시 해 주세요.",
    "ERROR-500": "나이스 서버에 문제가 있어요. 잠시 뒤에 다시 해 주세요.",
    "ERROR-600": "나이스 서버에 문제가 있어요. 잠시 뒤에 다시 해 주세요.",
}


def friendly_error(code: str) -> str:
    return _ERRORS.get(code, f"나이스에서 오류가 났어요 ({code}).")
