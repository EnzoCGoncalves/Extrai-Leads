import re
import unicodedata
from hashlib import sha256
from urllib.parse import urlsplit, urlunsplit

from extrais_leads.services.whatsapp_evidence import normalize_brazilian_phone

_NON_WORD = re.compile(r"[^a-z0-9]+")
_WHITESPACE = re.compile(r"\s+")
_LEGAL_SUFFIXES = ("eireli", "ltda", "me", "epp")


def strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def normalize_text(value: str) -> str:
    """Normalize comparison text while source values remain untouched in storage."""

    return _WHITESPACE.sub(" ", strip_accents(value).casefold()).strip()


def normalize_query(value: str) -> str:
    return normalize_text(value)


def query_fingerprint(normalized_query: str) -> str:
    return sha256(normalized_query.encode("utf-8")).hexdigest()


def normalize_company_name(value: str) -> str:
    normalized = _NON_WORD.sub(" ", normalize_text(value)).strip()
    parts = normalized.split()
    while parts and parts[-1] in _LEGAL_SUFFIXES:
        parts.pop()
    return " ".join(parts)


def normalize_phone(value: str | None) -> str | None:
    """Normalize a Brazilian phone to country code + DDD + subscriber digits.

    The numbering-plan check only validates shape.  It never implies that the
    number is active or registered with WhatsApp.
    """

    return normalize_brazilian_phone(value)


def normalize_url(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    candidate = value.strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlsplit(candidate)
    if parsed.scheme.casefold() not in {"http", "https"}:
        return None
    if not parsed.hostname or parsed.username or parsed.password:
        return None
    hostname = parsed.hostname.casefold()
    try:
        parsed_port = parsed.port
    except ValueError:
        return None
    port = f":{parsed_port}" if parsed_port else ""
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.casefold(), hostname + port, path, parsed.query, ""))
