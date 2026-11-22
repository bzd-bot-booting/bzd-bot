
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class Notification(Base):
    __tablename__ = "notifications"

    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_notification_idempotency_key",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )

    monitor_key: Mapped[str] = mapped_column(
        ForeignKey("monitors.key", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    event_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )

    idempotency_key: Mapped[str] = mapped_column(
        String(255), nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(30), default="sent", nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
