"""merge booking jobs and encrypted monitor profile migration branches

Revision ID: e7b91d4a6c20
Revises: d4a8b2c91e70, f31a8c6d9e20
Create Date: 2026-11-16
"""

from typing import Sequence, Union


revision: str = "e7b91d4a6c20"
down_revision: Union[str, Sequence[str], None] = (
    "d4a8b2c91e70",
    "f31a8c6d9e20",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Объединение двух уже применяемых веток схемы.
    pass


def downgrade() -> None:
    # Merge revision не меняет схему; downgrade разделяет историю.
    pass
