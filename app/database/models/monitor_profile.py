from datetime import datetime

from sqlalchemy import BigInteger, DateTime, LargeBinary, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class MonitorProfile(Base):
    """Зашифрованный профиль для автоматического бронирования мониторинга."""

    __tablename__ = "monitor_profiles"

    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
    )

    # Fernet ciphertext: login, password and passenger records.
    encrypted_payload: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
