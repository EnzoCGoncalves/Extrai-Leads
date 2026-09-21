from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Enum, Index, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from extrais_leads.models.base import (
    Base,
    TimestampMixin,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    utc_now,
)
from extrais_leads.models.enums import ValidationStatus, WhatsAppStatus

if TYPE_CHECKING:
    from extrais_leads.models.search_result import SearchResult


class Company(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 100)",
            name="confidence_range",
        ),
        CheckConstraint(
            "(whatsapp_status != 'confirmed' OR whatsapp IS NOT NULL) AND "
            "(whatsapp IS NULL OR whatsapp_status != 'not_found')",
            name="whatsapp_status_consistency",
        ),
        Index("ix_companies_identity", "name_fingerprint", "city", "state"),
    )

    name: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    name_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    phone: Mapped[str | None] = mapped_column(String(32), index=True)
    whatsapp: Mapped[str | None] = mapped_column(String(32), index=True)
    whatsapp_status: Mapped[WhatsAppStatus] = mapped_column(
        Enum(
            WhatsAppStatus,
            native_enum=False,
            values_callable=lambda enum: [item.value for item in enum],
            validate_strings=True,
            length=20,
        ),
        default=WhatsAppStatus.NOT_FOUND,
        nullable=False,
    )
    validation_status: Mapped[ValidationStatus] = mapped_column(
        Enum(
            ValidationStatus,
            native_enum=False,
            values_callable=lambda enum: [item.value for item in enum],
            validate_strings=True,
            length=20,
        ),
        default=ValidationStatus.UNVERIFIED,
        nullable=False,
    )
    address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(150), index=True)
    state: Mapped[str | None] = mapped_column(String(100), index=True)
    category: Mapped[str | None] = mapped_column(String(200), index=True)
    website: Mapped[str | None] = mapped_column(Text)
    instagram: Mapped[str | None] = mapped_column(String(200))
    cnpj: Mapped[str | None] = mapped_column(String(14), index=True)
    confidence: Mapped[int | None] = mapped_column(SmallInteger)
    collected_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    search_results: Mapped[list["SearchResult"]] = relationship(
        back_populates="company", lazy="raise", passive_deletes=True
    )
