from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from extrais_leads.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from extrais_leads.models.enums import SourceType

if TYPE_CHECKING:
    from extrais_leads.models.provider_run import SearchProviderRun
    from extrais_leads.models.result_source import ResultSource


class Source(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sources"

    provider_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(150), nullable=False)
    source_type: Mapped[SourceType] = mapped_column(
        Enum(
            SourceType,
            native_enum=False,
            values_callable=lambda enum: [item.value for item in enum],
            validate_strings=True,
            length=20,
        ),
        default=SourceType.OTHER,
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    provider_runs: Mapped[list["SearchProviderRun"]] = relationship(
        back_populates="source", lazy="raise"
    )
    result_sources: Mapped[list["ResultSource"]] = relationship(
        back_populates="source", lazy="raise"
    )
