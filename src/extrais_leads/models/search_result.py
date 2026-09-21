import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from extrais_leads.models.base import (
    Base,
    TimestampMixin,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    utc_now,
)

if TYPE_CHECKING:
    from extrais_leads.models.company import Company
    from extrais_leads.models.contact_evidence import ContactEvidence
    from extrais_leads.models.result_source import ResultSource
    from extrais_leads.models.search import Search


class SearchResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "search_results"
    __table_args__ = (
        UniqueConstraint("search_id", "company_id", name="search_company"),
        CheckConstraint("rank IS NULL OR rank > 0", name="rank_positive"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 100)",
            name="confidence_range",
        ),
        CheckConstraint(
            "qualification_confidence IS NULL OR "
            "(qualification_confidence >= 0 AND qualification_confidence <= 100)",
            name="qualification_confidence_range",
        ),
    )

    search_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("searches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rank: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[int | None] = mapped_column(SmallInteger)
    category_match: Mapped[bool | None]
    qualification_confidence: Mapped[int | None] = mapped_column(SmallInteger)
    qualification_method: Mapped[str | None] = mapped_column(String(30))
    qualification_reason: Mapped[str | None] = mapped_column(Text)
    collected_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    search: Mapped["Search"] = relationship(back_populates="results", lazy="raise")
    company: Mapped["Company"] = relationship(back_populates="search_results", lazy="raise")
    sources: Mapped[list["ResultSource"]] = relationship(
        back_populates="search_result", cascade="all, delete-orphan", lazy="raise"
    )
    contact_evidences: Mapped[list["ContactEvidence"]] = relationship(
        back_populates="search_result", cascade="all, delete-orphan", lazy="raise"
    )
