import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    Index,
    LargeBinary,
    Numeric,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class BookingJob(Base):
    """
    Долговременная задача автоматического бронирования.

    Чувствительные данные хранятся только в sensitive_payload
    и должны записываться туда исключительно в зашифрованном виде.
    """

    __tablename__ = "booking_jobs"

    __table_args__ = (
        Index(
            "ix_booking_jobs_status_next_attempt",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_booking_jobs_lease_until",
            "lease_until",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        index=True,
    )

    from_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    to_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    travel_date: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    train_number: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )

    train_data: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    url: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    # draft, waiting, running, submitting, order_unknown,
    # order_created, cancelled, expired, failed
    status: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="draft",
        server_default=text("'draft'"),
        index=True,
    )

    # Текущий этап пользовательского или браузерного процесса.
    current_step: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
        default="created",
        server_default=text("'created'"),
    )

    preferences: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    # Только шифротекст. Открытые пароли и паспортные данные
    # в эту колонку записывать запрещено.
    sensitive_payload: Mapped[bytes | None] = mapped_column(
        LargeBinary,
        nullable=True,
    )

    # Несекретные сведения о созданном заказе.
    order_number: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    order_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Поля для восстановления и повторного выполнения задач.
    lease_owner: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )

    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )

    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Здесь допустимы только технические сообщения без секретов.
    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
