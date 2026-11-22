"""Add persistent automatic booking jobs.

Revision ID: d4a8b2c91e70
Revises: c8f2a91d6e40
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d4a8b2c91e70"
down_revision = "c8f2a91d6e40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "booking_jobs",

        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),

        sa.Column(
            "chat_id",
            sa.BigInteger(),
            nullable=False,
        ),

        sa.Column(
            "from_name",
            sa.String(length=255),
            nullable=False,
        ),

        sa.Column(
            "to_name",
            sa.String(length=255),
            nullable=False,
        ),

        sa.Column(
            "travel_date",
            sa.String(length=20),
            nullable=False,
        ),

        sa.Column(
            "train_number",
            sa.String(length=32),
            nullable=False,
        ),

        sa.Column(
            "train_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),

        sa.Column(
            "url",
            sa.Text(),
            nullable=False,
        ),

        sa.Column(
            "status",
            sa.String(length=40),
            server_default=sa.text("'draft'"),
            nullable=False,
        ),

        sa.Column(
            "current_step",
            sa.String(length=60),
            server_default=sa.text("'created'"),
            nullable=False,
        ),

        sa.Column(
            "preferences",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),

        sa.Column(
            "sensitive_payload",
            sa.LargeBinary(),
            nullable=True,
        ),

        sa.Column(
            "order_number",
            sa.String(length=100),
            nullable=True,
        ),

        sa.Column(
            "order_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),

        sa.Column(
            "lease_owner",
            sa.String(length=128),
            nullable=True,
        ),

        sa.Column(
            "lease_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),

        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),

        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),

        sa.Column(
            "last_error",
            sa.Text(),
            nullable=True,
        ),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),

        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),

        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_booking_jobs_chat_id",
        "booking_jobs",
        ["chat_id"],
    )

    op.create_index(
        "ix_booking_jobs_status",
        "booking_jobs",
        ["status"],
    )

    op.create_index(
        "ix_booking_jobs_status_next_attempt",
        "booking_jobs",
        ["status", "next_attempt_at"],
    )

    op.create_index(
        "ix_booking_jobs_lease_until",
        "booking_jobs",
        ["lease_until"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_booking_jobs_lease_until",
        table_name="booking_jobs",
    )

    op.drop_index(
        "ix_booking_jobs_status_next_attempt",
        table_name="booking_jobs",
    )

    op.drop_index(
        "ix_booking_jobs_status",
        table_name="booking_jobs",
    )

    op.drop_index(
        "ix_booking_jobs_chat_id",
        table_name="booking_jobs",
    )

    op.drop_table("booking_jobs")
