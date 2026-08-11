"""Widen data_point_series values for GPS precision.

Revision ID: f6a7b8c9d0e1
Revises: b2c3d4e5f6a1

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, None] = "b2c3d4e5f6a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Increasing precision and scale preserves existing NUMERIC(10,3) values.
    op.alter_column(
        "data_point_series",
        "value",
        existing_type=sa.Numeric(precision=10, scale=3),
        type_=sa.Numeric(precision=15, scale=6),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "data_point_series",
        "value",
        existing_type=sa.Numeric(precision=15, scale=6),
        type_=sa.Numeric(precision=10, scale=3),
        existing_nullable=False,
    )
