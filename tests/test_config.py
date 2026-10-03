from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from northstar.clock import FixedDateClock, FrozenClock, get_app_today
from northstar.config import ConfigurationError, Settings, require_database_url


def make(**env):
    base = {"APP_TODAY": "2026-10-19"}
    base.update(env)
    return Settings(_env_file=None, **base)


def test_app_today_is_the_fixed_business_date():
    assert get_app_today() == date(2026, 10, 19)


def test_app_today_is_required(monkeypatch):
    monkeypatch.delenv("APP_TODAY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_invalid_app_today_rejected():
    with pytest.raises(ValidationError):
        make(APP_TODAY="19/10/2026")


def test_fixed_clock_now_falls_on_business_date():
    clock = FixedDateClock(date(2026, 10, 19), ZoneInfo("Asia/Tbilisi"))
    now = clock.now()
    assert now.date() == date(2026, 10, 19)
    assert now.tzinfo is not None


def test_frozen_clock():
    moment = datetime(2026, 10, 19, 11, 0, tzinfo=ZoneInfo("Asia/Tbilisi"))
    clock = FrozenClock(moment)
    assert clock.today() == date(2026, 10, 19)
    assert clock.now() == moment


def test_demo_employee_id_normalised_and_validated():
    assert make(DEMO_EMPLOYEE_ID=" e1001 ").demo_employee_id == "E1001"
    assert make(DEMO_EMPLOYEE_ID="").demo_employee_id is None
    with pytest.raises(ValidationError):
        make(DEMO_EMPLOYEE_ID="1001; drop table")


@pytest.mark.parametrize("schema", ["Public", "a-b", "x;drop", "1abc"])
def test_db_schema_must_be_identifier(schema):
    with pytest.raises(ValidationError):
        make(DB_SCHEMA=schema)


def test_invalid_timezone_rejected():
    with pytest.raises(ValidationError):
        make(APP_TIMEZONE="Mars/Olympus")


def test_secrets_never_appear_in_repr():
    settings = make(
        DATABASE_URL="postgresql://u:supersecretpw@host/db",
        GEMINI_API_KEY="AIza-secret-key",
        SUPABASE_SERVICE_ROLE_KEY="service-secret",
    )
    rendered = repr(settings) + str(settings.model_dump())
    for secret in ("supersecretpw", "AIza-secret-key", "service-secret"):
        assert secret not in rendered


def test_missing_database_url_gives_safe_error():
    with pytest.raises(ConfigurationError) as exc:
        require_database_url(make(DATABASE_URL=""))
    assert "DATABASE_URL" in str(exc.value)


def test_defaults():
    s = make()
    assert s.gemini_model == "gemini-3.5-flash-lite"
    assert s.embedding_model == "gemini-embedding-001"
    assert s.embedding_dim == 768
    assert s.db_schema == "public"
