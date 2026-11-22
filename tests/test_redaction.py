from app.core.redaction import redact_sensitive_text


def test_redacts_password_and_token_fields():
    text = "password=hunter2 telegram_token=123456:abcdefghijklmnopqrstuvwxyzABCDEF"
    result = redact_sensitive_text(text)
    assert "hunter2" not in result
    assert "123456:abcdefghijklmnopqrstuvwxyzABCDEF" not in result


def test_redacts_passenger_fields_and_database_password():
    text = "passport=AB123456 phone=+37060000000 db=postgresql://user:secret@db:5432/app"
    result = redact_sensitive_text(text)
    assert "AB123456" not in result
    assert "+37060000000" not in result
    assert "user:secret@" not in result
