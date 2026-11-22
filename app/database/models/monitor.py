
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class Monitor(Base):
    __tablename__ = "monitors"

    key: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
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

    train: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
    )

    url: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    # Общее количество распределённых мест.
    # Оставляем поле для совместимости и статистики.
    allocated_seats: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    # Распределение мест по отдельным тарифам.
    # Ключ тарифа формируется из категории, типа вагона и цены.
    allocated_fares: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
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
