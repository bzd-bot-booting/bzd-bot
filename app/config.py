from dataclasses import dataclass
import os

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    database_url: str
    booking_encryption_key: str

    check_interval: int = 30
    headless: bool = True
    page_timeout_ms: int = 90_000
    page_wait_ms: int = 2_000
    max_tickets_per_request: int = 9
    admin_chat_ids: tuple[int, ...] = ()
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    email_from: str = ""
    email_to: str = ""
    smtp_use_ssl: bool = False
    smtp_starttls: bool = True
    smtp_timeout_seconds: int = 15
    smtp_error_cooldown_seconds: int = 900
    report_timezone: str = "Europe/Vilnius"
    report_hour: int = 9
    report_minute: int = 0


def load_settings() -> Settings:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN. Создайте .env на основе .env.example."
        )

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError(
            "Не задан DATABASE_URL. Добавьте строку подключения PostgreSQL в .env."
        )

    encryption_key = os.getenv("BOOKING_ENCRYPTION_KEY", "").strip()
    if not encryption_key:
        raise RuntimeError(
            "Не задан BOOKING_ENCRYPTION_KEY. "
            "Создайте ключ шифрования и добавьте его в .env."
        )

    return Settings(
        telegram_token=token,
        database_url=database_url,
        booking_encryption_key=encryption_key,
        check_interval=max(10, int(os.getenv("CHECK_INTERVAL", "30"))),
        headless=os.getenv("PLAYWRIGHT_HEADLESS", "true").lower()
        not in {"0", "false", "no"},
        page_timeout_ms=max(10_000, int(os.getenv("PAGE_TIMEOUT_MS", "90000"))),
        page_wait_ms=max(0, int(os.getenv("PAGE_WAIT_MS", "2000"))),
        max_tickets_per_request=max(
            1, int(os.getenv("MAX_TICKETS_PER_REQUEST", "9"))
        ),
        admin_chat_ids=tuple(
            int(value.strip())
            for value in os.getenv("ADMIN_CHAT_IDS", "").split(",")
            if value.strip()
        ),
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", "").strip().replace(" ", ""),
        email_from=os.getenv("EMAIL_FROM", "").strip(),
        email_to=os.getenv("EMAIL_TO", "").strip(),
        smtp_use_ssl=os.getenv("SMTP_USE_SSL", "false").lower() in {"1", "true", "yes"},
        smtp_starttls=os.getenv("SMTP_STARTTLS", "true").lower() in {"1", "true", "yes"},
        smtp_timeout_seconds=max(5, int(os.getenv("SMTP_TIMEOUT_SECONDS", "15"))),
        smtp_error_cooldown_seconds=max(60, int(os.getenv("SMTP_ERROR_COOLDOWN_SECONDS", "900"))),
        report_timezone=os.getenv("REPORT_TIMEZONE", "Europe/Vilnius").strip(),
        report_hour=min(23, max(0, int(os.getenv("REPORT_HOUR", "9")))),
        report_minute=min(59, max(0, int(os.getenv("REPORT_MINUTE", "0")))),
    )
