"""add search progress metrics

Revision ID: b7c4e91d2a6f
Revises: 83223a41a984
Create Date: 2026-09-17 17:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c4e91d2a6f"
down_revision: str | Sequence[str] | None = "83223a41a984"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("searches", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "discovered_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "progress_percent",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.create_check_constraint(
            batch_op.f("ck_searches_discovered_count_non_negative"),
            "discovered_count >= 0",
        )
        batch_op.create_check_constraint(
            batch_op.f("ck_searches_progress_percent_range"),
            "progress_percent >= 0 AND progress_percent <= 100",
        )


def downgrade() -> None:
    with op.batch_alter_table("searches", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("ck_searches_progress_percent_range"), type_="check")
        batch_op.drop_constraint(
            batch_op.f("ck_searches_discovered_count_non_negative"), type_="check"
        )
        batch_op.drop_column("progress_percent")
        batch_op.drop_column("discovered_count")
