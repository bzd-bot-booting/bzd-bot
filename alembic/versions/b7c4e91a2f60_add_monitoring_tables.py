"""Add persistent monitoring, subscriptions and notifications.

Revision ID: b7c4e91a2f60
Revises: d01099383372
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b7c4e91a2f60"
down_revision: Union[str, Sequence[str], None] = "d01099383372"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Убираем ошибочный DEFAULT у Telegram ID.
    op.execute(
        "ALTER TABLE users ALTER COLUMN telegram_id DROP DEFAULT"
    )

    # Если старая последовательность существует, отвязываем и удаляем её.
    op.execute("""
        DO $$
        BEGIN
            IF to_regclass('public.users_telegram_id_seq') IS NOT NULL THEN
                ALTER SEQUENCE public.users_telegram_id_seq OWNED BY NONE;
                DROP SEQUENCE public.users_telegram_id_seq;
            END IF;
        END
        $$;
    """)

    op.create_table(
        "monitors",
        sa.Column("key", sa.String(length=255), primary_key=True),
        sa.Column("from_name", sa.String(length=255), nullable=False),
        sa.Column("to_name", sa.String(length=255), nullable=False),
        sa.Column("travel_date", sa.String(length=20), nullable=False),
        sa.Column("train", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "allocated_seats",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "subscriptions",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "monitor_key",
            sa.String(length=255),
            sa.ForeignKey("monitors.key", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("tickets_count", sa.Integer(), nullable=False),
        sa.Column("created_order", sa.Integer(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "monitor_key",
            "chat_id",
            name="uq_subscription_monitor_chat",
        ),
    )

    op.create_index(
        "ix_subscriptions_monitor_key",
        "subscriptions",
        ["monitor_key"],
    )
    op.create_index(
        "ix_subscriptions_chat_id",
        "subscriptions",
        ["chat_id"],
    )
    op.create_index(
        "ix_subscriptions_is_active",
        "subscriptions",
        ["is_active"],
    )

    op.create_table(
        "notifications",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "monitor_key",
            sa.String(length=255),
            sa.ForeignKey("monitors.key", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=30),
            nullable=False,
            server_default="sent",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_notification_idempotency_key",
        ),
    )

    op.create_index(
        "ix_notifications_monitor_key",
        "notifications",
        ["monitor_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notifications_monitor_key",
        table_name="notifications",
    )
    op.drop_table("notifications")

    op.drop_index(
        "ix_subscriptions_is_active",
        table_name="subscriptions",
    )
    op.drop_index(
        "ix_subscriptions_chat_id",
        table_name="subscriptions",
    )
    op.drop_index(
        "ix_subscriptions_monitor_key",
        table_name="subscriptions",
    )
    op.drop_table("subscriptions")

    op.drop_table("monitors")

    # Исправление telegram_id намеренно не откатывается:
    # автоматическая генерация Telegram ID не нужна.
