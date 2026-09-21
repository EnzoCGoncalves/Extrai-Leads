from enum import StrEnum


class SearchStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SearchStage(StrEnum):
    CREATED = "created"
    DISCOVERING = "discovering"
    ENRICHING = "enriching"
    VALIDATING = "validating"
    DEDUPLICATING = "deduplicating"
    FINALIZING = "finalizing"
    COMPLETED = "completed"


class WhatsAppStatus(StrEnum):
    NOT_FOUND = "not_found"
    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"


class ValidationStatus(StrEnum):
    UNVERIFIED = "unverified"
    VALID = "valid"
    INVALID = "invalid"


class SourceType(StrEnum):
    SEARCH = "search"
    DIRECTORY = "directory"
    REGISTRY = "registry"
    WEBSITE = "website"
    AI = "ai"
    OTHER = "other"


class ProviderRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
