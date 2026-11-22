import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://test:test@127.0.0.1:5432/test",
)
os.environ.setdefault(
    "BOOKING_ENCRYPTION_KEY",
    "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
)

import pytest

from app.services.monitor_profiles import MonitorProfileService


def valid_profile():
    return {
        "login": "test-login",
        "password": "test-password",
        "passengers": [
            {
                "last_name": "Иванов",
                "first_name": "Иван",
                "document_type": "Паспорт гражданина РБ",
                "document_number": "MP0000000",
            }
        ],
    }


def test_profile_accepts_complete_data():
    MonitorProfileService._validate(valid_profile(), require_passengers=True)


def test_profile_allows_credentials_before_passenger_data():
    profile = valid_profile()
    profile["passengers"] = []
    MonitorProfileService._validate(profile)


def test_profile_rejects_missing_credentials():
    profile = valid_profile()
    profile["password"] = ""
    with pytest.raises(ValueError):
        MonitorProfileService._validate(profile)


def test_profile_rejects_incomplete_passenger():
    profile = valid_profile()
    profile["passengers"][0]["document_number"] = ""
    with pytest.raises(ValueError):
        MonitorProfileService._validate(profile, require_passengers=True)
