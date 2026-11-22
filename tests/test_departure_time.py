import os
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.setdefault("TRAIN_TIMEZONE", "Europe/Minsk")

from app.services.rw_parser import is_departure_time_passed


def test_departure_is_detected_after_scheduled_time():
    now = datetime(2026, 11, 22, 14, 0, tzinfo=ZoneInfo("Europe/Minsk"))
    assert is_departure_time_passed("2026-11-20", "18:54", now=now)


def test_departure_is_not_detected_before_scheduled_time():
    now = datetime(2026, 11, 20, 18, 53, tzinfo=ZoneInfo("Europe/Minsk"))
    assert not is_departure_time_passed("2026-11-20", "18:54", now=now)


def test_departure_parser_accepts_time_inside_text():
    now = datetime(2026, 11, 20, 18, 55, tzinfo=ZoneInfo("Europe/Minsk"))
    assert is_departure_time_passed("2026-11-20", "Отправление 18:54", now=now)


def test_departure_parser_ignores_missing_time():
    now = datetime(2026, 11, 22, 14, 0, tzinfo=ZoneInfo("Europe/Minsk"))
    assert not is_departure_time_passed("2026-11-20", "--:--", now=now)
