import os

# 진짜 .env 값(나이스 인증키) 대신 시험용 값을 먼저 넣어 둬요. load_dotenv 는 이미 있는 값을 덮어쓰지 않아요.
os.environ.update({
    "NEIS_API_KEY": "test-key",
    "NEIS_ATPT_OFCDC_SC_CODE": "B10",
    "NEIS_SD_SCHUL_CODE": "7010000",
    "TEACHER_PASSWORD": "teacher-pw",
    "NEIS_AUTO_SYNC": "0",  # 시험 중에는 자동 불러오기를 끄고 직접 불러요
})

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from app.main import app
    with TestClient(app) as c:
        yield c
