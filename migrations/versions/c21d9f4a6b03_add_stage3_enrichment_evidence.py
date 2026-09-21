"""add stage 3 enrichment and qualification evidence

Revision ID: c21d9f4a6b03
Revises: b7c4e91d2a6f
Create Date: 2026-09-18 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c21d9f4a6b03"
down_revision: str | Sequence[str] | None = "b7c4e91d2a6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("searches", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "confirmed_whatsapp_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "enriched_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "ai_qualified_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.create_check_constraint(
            batch_op.f("ck_searches_confirmed_whatsapp_count_non_negative"),
            "confirmed_whatsapp_count >= 0",
        )
        batch_op.create_check_constraint(
            batch_op.f("ck_searches_enriched_count_non_negative"),
            "enriched_count >= 0",
        )
        batch_op.create_check_constraint(
            batch_op.f("ck_searches_ai_qualified_count_non_negative"),
            "ai_qualified_count >= 0",
        )

    with op.batch_alter_table("search_results", schema=None) as batch_op:
        batch_op.add_column(sa.Column("qualification_confidence", sa.SmallInteger()))
        batch_op.add_column(sa.Column("qualification_method", sa.String(length=30)))
        batch_op.create_check_constraint(
            batch_op.f("ck_search_results_qualification_confidence_range"),
            "qualification_confidence IS NULL OR "
            "(qualification_confidence >= 0 AND qualification_confidence <= 100)",
        )

    op.create_table(
        "contact_evidences",
        sa.Column("search_result_id", sa.Uuid(), nullable=False),
        sa.Column("contact_type", sa.String(length=30), nullable=False),
        sa.Column("normalized_value", sa.String(length=32), nullable=False),
        sa.Column("evidence_type", sa.String(length=60), nullable=False),
        sa.Column("source_provider", sa.String(length=100), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("official_source", sa.Boolean(), nullable=False),
        sa.Column("excerpt", sa.Text()),
        sa.Column("details", sa.JSON()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["search_result_id"],
            ["search_results.id"],
            name=op.f("fk_contact_evidences_search_result_id_search_results"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contact_evidences")),
        sa.UniqueConstraint(
            "search_result_id",
            "contact_type",
            "normalized_value",
            "evidence_type",
            "source_url",
            name="result_contact_evidence",
        ),
    )
    with op.batch_alter_table("contact_evidences", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_contact_evidences_normalized_value"),
            ["normalized_value"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_contact_evidences_search_result_id"),
            ["search_result_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("contact_evidences", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_contact_evidences_search_result_id"))
        batch_op.drop_index(batch_op.f("ix_contact_evidences_normalized_value"))
    op.drop_table("contact_evidences")

    with op.batch_alter_table("search_results", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("ck_search_results_qualification_confidence_range"),
            type_="check",
        )
        batch_op.drop_column("qualification_method")
        batch_op.drop_column("qualification_confidence")

    with op.batch_alter_table("searches", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("ck_searches_ai_qualified_count_non_negative"), type_="check"
        )
        batch_op.drop_constraint(
            batch_op.f("ck_searches_enriched_count_non_negative"), type_="check"
        )
        batch_op.drop_constraint(
            batch_op.f("ck_searches_confirmed_whatsapp_count_non_negative"),
            type_="check",
        )
        batch_op.drop_column("ai_qualified_count")
        batch_op.drop_column("enriched_count")
        batch_op.drop_column("confirmed_whatsapp_count")
