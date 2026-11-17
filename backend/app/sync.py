"""나이스 급식·학사일정 자동 불러오기.

서버가 켜져 있는 동안 매일 아침 9시(한국 시간)에 지난 30일(오늘 포함) 급식을 한 번에 불러와요.
- 아직 메뉴가 없는 날만 채워요. 한 번 채운 날은 선생님이 고친 대로 두고 다시 덮지 않아요.
- 9시에 컴퓨터가 꺼져 있거나 잠자기였으면, 서버가 켜지거나 깨어난 뒤 1분 안에 불러와요.
- 실패하면 30분 뒤에 다시 해 봐요 (최대 3번).
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from . import neis
from .db import connect, get_setting, set_setting

log = logging.getLogger("lunch.sync")
KST = ZoneInfo("Asia/Seoul")
RETRY_MINUTES = 30
SCHEDULE_AHEAD_DAYS = 30
MAX_RETRIES = 3


def sync_hour() -> int:
    return int(os.environ.get("NEIS_SYNC_HOUR", "9"))


def sync_days() -> int:
    return int(os.environ.get("NEIS_SYNC_DAYS", "30"))


def now_kst() -> datetime:
    return datetime.now(KST)


def next_run_after(now: datetime) -> datetime:
    """now 다음에 오는 아침 9시"""
    run = now.replace(hour=sync_hour(), minute=0, second=0, microsecond=0)
    return run if run > now else run + timedelta(days=1)


def last_status() -> dict | None:
    with connect() as conn:
        raw = get_setting(conn, "neis_last_sync")
    return json.loads(raw) if raw else None


def _save_status(status: dict) -> None:
    with connect() as conn:
        set_setting(conn, "neis_last_sync", json.dumps(status, ensure_ascii=False))


def save_days(days: dict[str, list[str]]) -> list[str]:
    """나이스에서 받은 날짜별 메뉴를 저장하고, 새로 채운 날짜 목록을 돌려줘요."""
    added = []
    with connect() as conn:
        for date, names in days.items():
            if conn.execute("SELECT 1 FROM neis_days WHERE date = ?", (date,)).fetchone():
                continue  # 이미 가져온 날
            has_menus = conn.execute("SELECT 1 FROM menus WHERE date = ?", (date,)).fetchone()
            if not has_menus:  # 선생님이 직접 넣은 날은 건드리지 않아요
                for order, name in enumerate(names):
                    conn.execute(
                        "INSERT INTO menus(date, name, ord, source) VALUES(?, ?, ?, 'neis')",
                        (date, name[:30], order),
                    )
                added.append(date)
            conn.execute("INSERT INTO neis_days(date) VALUES(?)", (date,))
    return added


def save_events(date_from: str, date_to: str, events: list[dict]) -> None:
    """그 기간의 학사일정을 새로 받은 것으로 바꿔요 (일정은 바뀔 수 있어서 매번 새로)."""
    with connect() as conn:
        conn.execute("DELETE FROM school_events WHERE date BETWEEN ? AND ?", (date_from, date_to))
        conn.executemany(
            "INSERT INTO school_events(date, name, no_school) VALUES(?, ?, ?)",
            [(e["date"], e["name"], int(e["no_school"])) for e in events],
        )


async def _fetch_events(api_key, office, school, date_from, date_to) -> list[dict] | None:
    url = neis.build_schedule_url(api_key, office, school, date_from, date_to, os.environ.get("NEIS_API_BASE"))
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(url)
            res.raise_for_status()
            result = neis.extract_events(res.json())
    except Exception as err:
        log.error("NEIS schedule fetch failed: %s", type(err).__name__)
        return None
    if result["status"] == "error":
        log.warning("NEIS schedule error: %s", result["code"])
        return None
    return result["events"]


async def run_sync(now: datetime | None = None) -> dict:
    """지금 바로 한 번 불러와요. 결과는 settings 의 neis_last_sync 에 남아요."""
    now = now or now_kst()
    date_to = now.strftime("%Y-%m-%d")
    date_from = (now - timedelta(days=sync_days() - 1)).strftime("%Y-%m-%d")
    status = {"at": now.isoformat(timespec="seconds"), "from": date_from, "to": date_to}

    api_key = os.environ.get("NEIS_API_KEY", "")
    office = os.environ.get("NEIS_ATPT_OFCDC_SC_CODE", "")
    school = os.environ.get("NEIS_SD_SCHUL_CODE", "")
    if not (api_key and office and school):
        status.update(ok=False, message="backend/.env 에 NEIS_API_KEY, NEIS_ATPT_OFCDC_SC_CODE, NEIS_SD_SCHUL_CODE 를 모두 채우고 서버를 다시 켜 주세요.")
        _save_status(status)
        return status

    url = neis.build_meal_url(api_key, office, school, date_from, date_to, os.environ.get("NEIS_API_BASE"))
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(url)
            res.raise_for_status()
            data = res.json()
    except Exception as err:  # 주소에 인증키가 들어 있으니 주소는 기록하지 않아요
        log.error("NEIS fetch failed: %s", type(err).__name__)
        status.update(ok=False, message="나이스에 연결하지 못했어요. 인터넷 연결을 확인해 주세요.")
        _save_status(status)
        return status

    result = neis.extract_lunches(data)
    if result["status"] == "error":
        log.warning("NEIS error: %s", result["code"])
        status.update(ok=False, message=neis.friendly_error(result["code"]))
        _save_status(status)
        return status

    added = save_days(result["days"])
    status.update(ok=True, meal_days=len(result["days"]), added=added,
                  message=f"급식 {len(result['days'])}일치를 확인하고 {len(added)}일치를 새로 채웠어요.")

    # 학사일정: 지난 30일 ~ 앞으로 30일. 실패해도 급식 결과는 그대로 둬요.
    sched_from, sched_to = date_from, (now + timedelta(days=SCHEDULE_AHEAD_DAYS)).strftime("%Y-%m-%d")
    events = await _fetch_events(api_key, office, school, sched_from, sched_to)
    if events is None:
        status["message"] += " 학사일정은 불러오지 못했어요."
    else:
        save_events(sched_from, sched_to, events)
        status["events"] = len(events)
        status["message"] += f" 학사일정 {len(events)}개도 확인했어요."
    _save_status(status)
    log.info("NEIS sync ok: %s", status["message"])
    return status


def synced_today(now: datetime) -> bool:
    status = last_status()
    return bool(status and status.get("ok") and status.get("to") == now.strftime("%Y-%m-%d"))


def due(now: datetime, last_try: datetime | None, tries_today: int) -> bool:
    """지금 불러와야 하는지.
    - 오늘 아직 성공하지 못했고
    - 아침 9시가 지났거나, 지금까지 한 번도 성공한 적이 없고 (처음 켰을 때)
    - 실패했다면 30분이 지났고, 오늘 시도가 1 + 3번(다시 시도)을 넘지 않았을 때"""
    if synced_today(now):
        return False
    status = last_status()
    never_ok = not (status and status.get("ok")) and tries_today == 0
    if now.hour < sync_hour() and not never_ok:
        return False
    if last_try and now - last_try < timedelta(minutes=RETRY_MINUTES):
        return False
    return tries_today <= MAX_RETRIES


async def scheduler() -> None:
    """서버가 켜져 있는 동안 1분마다 확인해요.
    맥이 잠자기에서 깨어나도 1분 안에 놓친 불러오기를 해요."""
    last_try: datetime | None = None
    tries_today, day = 0, None
    while True:
        now = now_kst()
        if now.date() != day:
            day, tries_today, last_try = now.date(), 0, None
        if due(now, last_try, tries_today):
            last_try = now
            tries_today += 1
            await _safe_run()
        await asyncio.sleep(60)


async def _safe_run() -> dict:
    try:
        return await run_sync()
    except Exception:  # 자동 작업이 죽어도 서버는 계속 돌게
        log.exception("NEIS sync crashed")
        return {"ok": False}


def next_scheduled() -> str:
    return next_run_after(now_kst()).isoformat(timespec="minutes")
