"""add encrypted monitor profiles

Revision ID: f31a8c6d9e20
Revises: c8f2a91d6e40
Create Date: 2026-11-16
"""

from alembic import op
import sqlalchemy as sa

revision = "f31a8c6d9e20"
down_revision = "c8f2a91d6e40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "monitor_profiles",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("encrypted_payload", sa.LargeBinary(), nullable=False),
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
    )


def downgrade() -> None:
    op.drop_table("monitor_profiles")
