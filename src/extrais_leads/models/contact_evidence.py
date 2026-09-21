import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, String, Text, UniqueConstraint, Uuid
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


class ContactEvidence(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Auditable public evidence for a contact attached to one search result."""

    __tablename__ = "contact_evidences"
    __table_args__ = (
        UniqueConstraint(
            "search_result_id",
            "contact_type",
            "normalized_value",
            "evidence_type",
            "source_url",
            name="result_contact_evidence",
        ),
    )

    search_result_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("search_results.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contact_type: Mapped[str] = mapped_column(String(30), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    evidence_type: Mapped[str] = mapped_column(String(60), nullable=False)
    source_provider: Mapped[str] = mapped_column(String(100), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    official_source: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    excerpt: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None]
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    search_result: Mapped["SearchResult"] = relationship(
        back_populates="contact_evidences", lazy="raise"
    )
