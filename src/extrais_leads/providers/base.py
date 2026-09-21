from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class ProviderSearchRequest(BaseModel):
    """Provider-neutral search input."""

    query: str = Field(min_length=2, max_length=500)
    category: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    max_results: int | None = Field(default=None, ge=1)
    cursor: str | None = None

    @field_validator("query", "category", "location", "cursor", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ProviderLead(BaseModel):
    """Raw but validated lead returned by one public data provider."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=300)
    phone: str | None = Field(default=None, max_length=64)
    whatsapp: str | None = Field(default=None, max_length=64)
    whatsapp_confirmed: bool = False
    whatsapp_evidence: str | None = Field(default=None, max_length=1000)
    address: str | None = None
    city: str | None = Field(default=None, max_length=150)
    state: str | None = Field(default=None, max_length=100)
    category: str | None = Field(default=None, max_length=200)
    website: HttpUrl | None = None
    instagram: str | None = Field(default=None, max_length=200)
    cnpj: str | None = Field(default=None, max_length=32)
    source_url: HttpUrl | None = None
    external_id: str | None = Field(default=None, max_length=300)
    evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "name",
        "phone",
        "whatsapp",
        "whatsapp_evidence",
        "address",
        "city",
        "state",
        "category",
        "instagram",
        "cnpj",
        "external_id",
        mode="before",
    )
    @classmethod
    def strip_strings(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def require_evidence_for_confirmed_whatsapp(self) -> "ProviderLead":
        if self.whatsapp_confirmed and not (
            self.whatsapp and self.whatsapp_evidence and self.source_url
        ):
            raise ValueError("confirmed WhatsApp requires a number, public evidence and source_url")
        return self


class ProviderPage(BaseModel):
    """One provider page plus its opaque continuation cursor."""

    items: list[ProviderLead] = Field(default_factory=list)
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    pagination: bool = False
    query_variations: bool = False
    phone: bool = False
    whatsapp_evidence: bool = False
    enrichment: bool = False


class ProviderError(RuntimeError):
    """Recoverable provider failure handled by the future orchestrator."""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable
        self.retry_after = retry_after


class ProviderAuthenticationError(ProviderError):
    def __init__(self, provider: str, message: str = "provider authentication failed") -> None:
        super().__init__(provider, message, retryable=False)


class ProviderRateLimitError(ProviderError):
    def __init__(
        self,
        provider: str,
        message: str = "provider rate limit reached",
        *,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(provider, message, retryable=True, retry_after=retry_after)


class ProviderResponseError(ProviderError):
    pass


class SearchProvider(ABC):
    """Stable contract implemented by every external business-search source."""

    name: str
    capabilities = ProviderCapabilities()

    @property
    @abstractmethod
    def configured(self) -> bool:
        """Return whether credentials/configuration required by this provider exist."""
        raise NotImplementedError

    @abstractmethod
    async def search(self, request: ProviderSearchRequest) -> ProviderPage:
        """Return an evidence-backed page; cursors remain opaque to the orchestrator."""
        raise NotImplementedError

    async def healthcheck(self) -> bool:
        return self.configured

    async def close(self) -> None:
        """Release resources owned by the provider, if any."""
        return None
