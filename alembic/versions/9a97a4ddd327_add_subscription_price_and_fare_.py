
"""add subscription price and fare allocations

Revision ID: c8f2a91d6e40
Revises: b7c4e91a2f60
Create Date: 2026-11-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "c8f2a91d6e40"
down_revision = "b7c4e91a2f60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # У каждой подписки появляется собственный ценовой лимит.
    # NULL временно допускается для старых записей.
    op.add_column(
        "subscriptions",
        sa.Column(
            "max_price",
            sa.Numeric(10, 2),
            nullable=True,
        ),
    )

    # Храним распределение мест отдельно по каждому тарифу.
    op.add_column(
        "monitors",
        sa.Column(
            "allocated_fares",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )

    # У старых подписок нет достоверного ценового ограничения.
    # Не назначаем им выдуманную цену: деактивируем старые заявки.
    op.execute(
        """
        UPDATE subscriptions
        SET is_active = FALSE
        WHERE is_active = TRUE
        """
    )

    # После деактивации старых заявок старые мониторинги
    # также больше не должны запускаться автоматически.
    op.execute(
        """
        UPDATE monitors
        SET is_active = FALSE,
            allocated_fares = '{}'::jsonb,
            allocated_seats = 0
        """
    )

    # Новая активная подписка обязательно должна иметь цену.
    op.create_check_constraint(
        "ck_subscription_active_has_max_price",
        "subscriptions",
        "NOT is_active OR max_price IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_subscription_active_has_max_price",
        "subscriptions",
        type_="check",
    )

    op.drop_column("monitors", "allocated_fares")
    op.drop_column("subscriptions", "max_price")
