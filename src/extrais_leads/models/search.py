from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Enum, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from extrais_leads.models.base import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin
from extrais_leads.models.enums import SearchStage, SearchStatus

if TYPE_CHECKING:
    from extrais_leads.models.provider_run import SearchProviderRun
    from extrais_leads.models.search_result import SearchResult


class Search(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "searches"
    __table_args__ = (
        CheckConstraint("results_count >= 0", name="results_count_non_negative"),
        CheckConstraint("discovered_count >= 0", name="discovered_count_non_negative"),
        CheckConstraint("whatsapp_count >= 0", name="whatsapp_count_non_negative"),
        CheckConstraint(
            "confirmed_whatsapp_count >= 0",
            name="confirmed_whatsapp_count_non_negative",
        ),
        CheckConstraint("enriched_count >= 0", name="enriched_count_non_negative"),
        CheckConstraint("ai_qualified_count >= 0", name="ai_qualified_count_non_negative"),
        CheckConstraint(
            "progress_percent >= 0 AND progress_percent <= 100",
            name="progress_percent_range",
        ),
        Index("ix_searches_query_fingerprint_created_at", "query_fingerprint", "created_at"),
    )

    original_query: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_query: Mapped[str] = mapped_column(Text, nullable=False)
    query_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str | None] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[SearchStatus] = mapped_column(
        Enum(
            SearchStatus,
            native_enum=False,
            values_callable=lambda enum: [item.value for item in enum],
            validate_strings=True,
            length=20,
        ),
        default=SearchStatus.CREATED,
        nullable=False,
        index=True,
    )
    stage: Mapped[SearchStage] = mapped_column(
        Enum(
            SearchStage,
            native_enum=False,
            values_callable=lambda enum: [item.value for item in enum],
            validate_strings=True,
            length=20,
        ),
        default=SearchStage.CREATED,
        nullable=False,
    )
    results_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    discovered_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    whatsapp_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    confirmed_whatsapp_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enriched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ai_qualified_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    progress_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    results: Mapped[list["SearchResult"]] = relationship(
        back_populates="search", cascade="all, delete-orphan", lazy="raise"
    )
    provider_runs: Mapped[list["SearchProviderRun"]] = relationship(
        back_populates="search", cascade="all, delete-orphan", lazy="raise"
    )
