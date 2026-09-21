import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from extrais_leads.models.enums import (
    ProviderRunStatus,
    SearchStage,
    SearchStatus,
    ValidationStatus,
    WhatsAppStatus,
)


class ProviderRunRead(BaseModel):
    provider: str
    display_name: str
    status: ProviderRunStatus
    results_count: int
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None


class SearchCreate(BaseModel):
    query: str | None = Field(default=None, min_length=2, max_length=500)
    category: str | None = Field(default=None, min_length=2, max_length=200)
    location: str | None = Field(default=None, min_length=2, max_length=200)

    @field_validator("query", "category", "location", mode="before")
    @classmethod
    def clean_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @model_validator(mode="after")
    def require_query_or_structured_criteria(self) -> "SearchCreate":
        if self.query:
            return self
        if self.category and self.location:
            return self
        raise ValueError("provide query or both category and location")

    @property
    def resolved_query(self) -> str:
        if self.query:
            return self.query
        return f"{self.category} em {self.location}"


class SearchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    query: str = Field(validation_alias="original_query")
    category: str | None
    location: str | None
    status: SearchStatus
    stage: SearchStage
    progress_percent: int
    discovered_count: int
    results_count: int
    companies_count: int
    whatsapp_count: int
    confirmed_whatsapp_count: int
    enriched_count: int
    ai_qualified_count: int
    error_message: str | None
    providers: list[ProviderRunRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class CompanyLeadRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    phone: str | None
    whatsapp: str | None
    whatsapp_status: WhatsAppStatus
    validation_status: ValidationStatus
    address: str | None
    city: str | None
    state: str | None
    category: str | None
    website: str | None
    instagram: str | None
    cnpj: str | None
    confidence: int | None
    collected_at: datetime


class SearchResultRead(BaseModel):
    id: uuid.UUID
    rank: int | None
    confidence: int | None
    category_match: bool | None
    qualification_confidence: int | None
    qualification_method: str | None
    qualification_reason: str | None
    collected_at: datetime
    company: CompanyLeadRead
    sources: list[str] = Field(default_factory=list)
    source_details: list["ResultSourceRead"] = Field(default_factory=list)
    whatsapp_evidence: list["ContactEvidenceRead"] = Field(default_factory=list)


class ResultSourceRead(BaseModel):
    provider: str
    display_name: str
    source_url: str | None
    external_id: str | None
    evidence: dict[str, object] | None


class ContactEvidenceRead(BaseModel):
    number: str
    evidence_type: str
    source: str
    source_url: str
    official_source: bool
    excerpt: str | None
    observed_at: datetime


class SearchResultsPage(BaseModel):
    items: list[SearchResultRead]
    total: int
    limit: int
    offset: int
