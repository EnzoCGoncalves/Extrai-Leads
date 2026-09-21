import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from extrais_leads.models.base import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin
from extrais_leads.models.enums import ProviderRunStatus

if TYPE_CHECKING:
    from extrais_leads.models.search import Search
    from extrais_leads.models.source import Source


class SearchProviderRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "search_provider_runs"
    __table_args__ = (
        UniqueConstraint("search_id", "source_id", name="search_source"),
        CheckConstraint("results_count >= 0", name="results_count_non_negative"),
    )

    search_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("searches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sources.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[ProviderRunStatus] = mapped_column(
        Enum(
            ProviderRunStatus,
            native_enum=False,
            values_callable=lambda enum: [item.value for item in enum],
            validate_strings=True,
            length=20,
        ),
        default=ProviderRunStatus.PENDING,
        nullable=False,
    )
    results_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    search: Mapped["Search"] = relationship(back_populates="provider_runs", lazy="raise")
    source: Mapped["Source"] = relationship(back_populates="provider_runs", lazy="raise")
