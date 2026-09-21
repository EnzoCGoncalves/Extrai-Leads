import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from extrais_leads.models.base import (
    Base,
    TimestampMixin,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    utc_now,
)

if TYPE_CHECKING:
    from extrais_leads.models.search_result import SearchResult
    from extrais_leads.models.source import Source


class ResultSource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Public evidence tying a lead to a provider and collection run."""

    __tablename__ = "result_sources"

    search_result_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("search_results.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sources.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    external_id: Mapped[str | None] = mapped_column(String(300))
    source_url: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict[str, Any] | None]
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    search_result: Mapped["SearchResult"] = relationship(back_populates="sources", lazy="raise")
    source: Mapped["Source"] = relationship(back_populates="result_sources", lazy="raise")
