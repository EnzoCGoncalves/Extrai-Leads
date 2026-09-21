"""Deterministic Brazilian phone normalization and public WhatsApp evidence.

This module deliberately does not contact WhatsApp or infer account availability.  A
``confirmed`` result means only that a public source explicitly identifies the number
as WhatsApp.  Whether the account still exists is a separate validation concern.
"""

from __future__ import annotations

import html
import ipaddress
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit, urlunsplit

from extrais_leads.models.enums import WhatsAppStatus

BRAZIL_COUNTRY_CODE = "55"

_VALID_AREA_CODES = frozenset(
    {
        "11",
        "12",
        "13",
        "14",
        "15",
        "16",
        "17",
        "18",
        "19",
        "21",
        "22",
        "24",
        "27",
        "28",
        "31",
        "32",
        "33",
        "34",
        "35",
        "37",
        "38",
        "41",
        "42",
        "43",
        "44",
        "45",
        "46",
        "47",
        "48",
        "49",
        "51",
        "53",
        "54",
        "55",
        "61",
        "62",
        "63",
        "64",
        "65",
        "66",
        "67",
        "68",
        "69",
        "71",
        "73",
        "74",
        "75",
        "77",
        "79",
        "81",
        "82",
        "83",
        "84",
        "85",
        "86",
        "87",
        "88",
        "89",
        "91",
        "92",
        "93",
        "94",
        "95",
        "96",
        "97",
        "98",
        "99",
    }
)

_EXTENSION_SUFFIX = re.compile(r"(?i)\s*(?:ramal|r\.?|ext(?:ens(?:a|ã)o)?|x)\s*[:.\-]?\s*\d+\s*$")
_PHONE_LIKE = re.compile(r"(?<!\d)(?:\+|00)?\d[\d\s().\-/]{6,25}\d(?!\d)")
_WHATSAPP_MARKER = re.compile(r"(?i)\bwhats\s*app\b")
_DIRECT_LINK = re.compile(
    r"(?i)(?:(?:https?:)?//)?(?:wa\.me|(?:api\.|web\.)?whatsapp\.com)/[^\s\"'<>]+"
)
_NEGATIVE_WHATSAPP = re.compile(
    r"(?i)(?:n[aã]o\s+(?:temos|possui|dispon[ií]vel).{0,20}whats\s*app|"
    r"whats\s*app\s+(?:n[aã]o\s+dispon[ií]vel|indispon[ií]vel))"
)


class BrazilianPhoneKind(StrEnum):
    """Numbering-plan category inferred from the subscriber number shape."""

    LANDLINE = "landline"
    MOBILE = "mobile"


@dataclass(frozen=True, slots=True)
class BrazilianPhone:
    """A syntactically valid Brazilian number in canonical components."""

    area_code: str
    subscriber_number: str
    kind: BrazilianPhoneKind
    country_code: str = BRAZIL_COUNTRY_CODE

    @property
    def national_number(self) -> str:
        return f"{self.area_code}{self.subscriber_number}"

    @property
    def digits(self) -> str:
        """Canonical storage form, including Brazil's country code and no punctuation."""

        return f"{self.country_code}{self.national_number}"

    @property
    def e164(self) -> str:
        return f"+{self.digits}"


class WhatsAppEvidenceType(StrEnum):
    """How a source represented a possible WhatsApp number."""

    DIRECT_LINK = "direct_link"
    EXPLICIT_LABEL = "explicit_label"
    STRUCTURED_FIELD = "structured_field"
    CLAIMED_NUMBER = "claimed_number"
    GENERIC_PHONE = "generic_phone"


@dataclass(frozen=True, slots=True)
class WhatsAppEvidence:
    """Raw evidence collected from a source without making a truth claim."""

    evidence_type: WhatsAppEvidenceType
    value: str
    source_url: str | None = None
    source: str | None = None
    official_source: bool = False
    context: str | None = None
    field_name: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedWhatsAppEvidence:
    """Auditable interpretation of one evidence item."""

    evidence_type: WhatsAppEvidenceType
    number: str
    source_url: str | None
    source: str | None
    official_source: bool
    context: str | None
    confirmed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class WhatsAppResolution:
    """Final status plus the evidence needed to explain it."""

    status: WhatsAppStatus
    phone: str | None
    whatsapp: str | None
    alternate_whatsapp_numbers: tuple[str, ...]
    evidence: tuple[ResolvedWhatsAppEvidence, ...]
    reason: str

    @property
    def is_confirmed(self) -> bool:
        return self.status is WhatsAppStatus.CONFIRMED


def parse_brazilian_phone(
    value: str | None,
    *,
    default_area_code: str | None = None,
) -> BrazilianPhone | None:
    """Parse one Brazilian number without checking whether it is active.

    Accepted forms include ``+55``, ``0055``, national DDD notation and Brazilian
    long-distance prefixes (``0 + carrier + DDD``).  A local 8/9 digit number is only
    accepted when an explicit ``default_area_code`` is supplied.
    """

    if not value or not value.strip():
        return None

    direct_number = extract_phone_from_whatsapp_link(value, default_area_code=default_area_code)
    if direct_number is not None:
        return direct_number

    without_extension = _EXTENSION_SUFFIX.sub("", html.unescape(value).strip())
    digits = "".join(character for character in without_extension if character in "0123456789")
    international_value = re.match(r"(?i)^\s*(?:tel:\s*)?\+", without_extension)
    if international_value and not digits.startswith(BRAZIL_COUNTRY_CODE):
        return None
    if digits.startswith("00") and not digits.startswith("0055"):
        return None
    national = _remove_brazil_dialing_prefix(digits)

    normalized_area_code = _normalize_area_code(default_area_code)
    if len(national) in {8, 9} and normalized_area_code:
        national = f"{normalized_area_code}{national}"

    if len(national) not in {10, 11}:
        return None

    area_code, subscriber = national[:2], national[2:]
    if area_code not in _VALID_AREA_CODES:
        return None

    if len(subscriber) == 9 and subscriber.startswith("9"):
        kind = BrazilianPhoneKind.MOBILE
    elif len(subscriber) == 8 and subscriber[0] in "2345":
        kind = BrazilianPhoneKind.LANDLINE
    else:
        return None

    return BrazilianPhone(area_code=area_code, subscriber_number=subscriber, kind=kind)


def normalize_brazilian_phone(
    value: str | None,
    *,
    default_area_code: str | None = None,
) -> str | None:
    """Return the canonical ``55 + DDD + number`` digits used for comparison/storage."""

    parsed = parse_brazilian_phone(value, default_area_code=default_area_code)
    return parsed.digits if parsed else None


def equivalent_brazilian_phones(
    left: str | None,
    right: str | None,
    *,
    default_area_code: str | None = None,
) -> bool:
    """Compare formatting variants without claiming that either number is active."""

    normalized_left = normalize_brazilian_phone(left, default_area_code=default_area_code)
    normalized_right = normalize_brazilian_phone(right, default_area_code=default_area_code)
    return normalized_left is not None and normalized_left == normalized_right


def extract_phone_from_whatsapp_link(
    value: str | None,
    *,
    default_area_code: str | None = None,
) -> BrazilianPhone | None:
    """Extract a number from official ``wa.me``/WhatsApp send URL shapes."""

    if not value or not value.strip():
        return None

    candidate = html.unescape(value).strip().strip("\"'<>.,;)")
    parsed_candidate = candidate
    lowered = candidate.casefold()
    if lowered.startswith(("wa.me/", "api.whatsapp.com/", "web.whatsapp.com/")):
        parsed_candidate = f"https://{candidate}"

    parsed = urlsplit(parsed_candidate)
    hostname = (parsed.hostname or "").casefold().removeprefix("www.")
    phone_value: str | None = None

    if hostname == "wa.me":
        path_part = parsed.path.strip("/").split("/", maxsplit=1)[0]
        if path_part.casefold() != "message":
            phone_value = path_part
    elif (
        hostname in {"api.whatsapp.com", "web.whatsapp.com", "whatsapp.com"}
        and parsed.path.rstrip("/").casefold() == "/send"
    ):
        phone_value = parse_qs(parsed.query).get("phone", [None])[0]
    elif parsed.scheme.casefold() == "whatsapp" and (
        hostname == "send" or parsed.path.strip("/").casefold() == "send"
    ):
        phone_value = parse_qs(parsed.query).get("phone", [None])[0]

    if not phone_value:
        return None

    # Avoid recursion by parsing the extracted digits directly.
    digits = "".join(character for character in phone_value if character in "0123456789")
    national = _remove_brazil_dialing_prefix(digits)
    area_code = _normalize_area_code(default_area_code)
    if len(national) in {8, 9} and area_code:
        national = f"{area_code}{national}"
    if len(national) not in {10, 11} or national[:2] not in _VALID_AREA_CODES:
        return None
    subscriber = national[2:]
    if len(subscriber) == 9 and subscriber.startswith("9"):
        kind = BrazilianPhoneKind.MOBILE
    elif len(subscriber) == 8 and subscriber[0] in "2345":
        kind = BrazilianPhoneKind.LANDLINE
    else:
        return None
    return BrazilianPhone(area_code=national[:2], subscriber_number=subscriber, kind=kind)


def extract_brazilian_phone_numbers(
    content: str,
    *,
    default_area_code: str | None = None,
) -> tuple[str, ...]:
    """Extract unique, syntactically valid Brazilian numbers in occurrence order."""

    numbers: list[str] = []
    seen: set[str] = set()
    for match in _PHONE_LIKE.finditer(html.unescape(content)):
        normalized = normalize_brazilian_phone(match.group(0), default_area_code=default_area_code)
        if normalized and normalized not in seen:
            seen.add(normalized)
            numbers.append(normalized)
    return tuple(numbers)


def extract_public_whatsapp_evidence(
    content: str,
    *,
    source_url: str,
    source: str | None = None,
    official_source: bool = False,
    default_area_code: str | None = None,
    max_content_chars: int = 1_000_000,
) -> tuple[WhatsAppEvidence, ...]:
    """Find bounded, explicit WhatsApp references in already-fetched public content.

    The function performs no network access.  Links and visible labelled text are
    inspected only within ``max_content_chars`` to keep enrichment work bounded.
    """

    public_source_url = _normalize_public_http_url(source_url)
    if public_source_url is None:
        raise ValueError("source_url must be a public HTTP(S) URL")
    if max_content_chars < 1:
        raise ValueError("max_content_chars must be positive")

    bounded_content = content[:max_content_chars]
    parser = _EvidenceHTMLParser()
    parser.feed(bounded_content)
    parser.close()
    visible_text = " ".join(parser.visible_parts)
    searchable_text = html.unescape(visible_text)

    found: list[WhatsAppEvidence] = []
    seen: set[tuple[WhatsAppEvidenceType, str]] = set()

    raw_links = [*parser.links]
    raw_links.extend(match.group(0) for match in _DIRECT_LINK.finditer(searchable_text))
    for raw_link in raw_links:
        direct_phone = extract_phone_from_whatsapp_link(
            raw_link, default_area_code=default_area_code
        )
        if direct_phone is None:
            continue
        key = (WhatsAppEvidenceType.DIRECT_LINK, direct_phone.digits)
        if key in seen:
            continue
        seen.add(key)
        found.append(
            WhatsAppEvidence(
                evidence_type=WhatsAppEvidenceType.DIRECT_LINK,
                value=raw_link,
                source_url=public_source_url,
                source=source,
                official_source=official_source,
                context="Public WhatsApp contact link",
            )
        )

    for marker in _WHATSAPP_MARKER.finditer(searchable_text):
        start = max(0, marker.start() - 80)
        end = min(len(searchable_text), marker.end() + 100)
        excerpt = " ".join(searchable_text[start:end].split())
        if _NEGATIVE_WHATSAPP.search(excerpt):
            continue
        nearest = _nearest_phone_to_marker(
            excerpt,
            marker_offset=marker.start() - start,
            default_area_code=default_area_code,
        )
        if nearest is None:
            continue
        key = (WhatsAppEvidenceType.EXPLICIT_LABEL, nearest)
        if key in seen:
            continue
        seen.add(key)
        found.append(
            WhatsAppEvidence(
                evidence_type=WhatsAppEvidenceType.EXPLICIT_LABEL,
                value=nearest,
                source_url=public_source_url,
                source=source,
                official_source=official_source,
                context=excerpt,
                field_name="WhatsApp",
            )
        )

    return tuple(found)


def resolve_whatsapp_status(
    *,
    phone: str | None = None,
    evidence: Iterable[WhatsAppEvidence] = (),
    default_area_code: str | None = None,
) -> WhatsAppResolution:
    """Resolve WhatsApp status without ever promoting an ordinary phone number.

    ``confirmed`` requires a normalized number, an explicit WhatsApp signal and a
    public evidence URL. ``unconfirmed`` represents a WhatsApp claim that lacks one
    of those requirements. A generic phone remains only ``phone`` and yields
    ``not_found`` when no WhatsApp-specific candidate exists.
    """

    normalized_phone = normalize_brazilian_phone(phone, default_area_code=default_area_code)
    records_by_number: dict[str, list[ResolvedWhatsAppEvidence]] = defaultdict(list)

    for item in evidence:
        if item.evidence_type is WhatsAppEvidenceType.GENERIC_PHONE:
            continue

        direct_phone = extract_phone_from_whatsapp_link(
            item.value, default_area_code=default_area_code
        )
        parsed = direct_phone or parse_brazilian_phone(
            item.value, default_area_code=default_area_code
        )
        if parsed is None:
            continue

        number = parsed.digits
        public_source_url = _normalize_public_http_url(item.source_url)
        direct_url = _direct_public_url(item.value) if direct_phone else None
        evidence_url = public_source_url or direct_url
        confirmed, reason = _evaluate_evidence(item, number, evidence_url, direct_phone)
        records_by_number[number].append(
            ResolvedWhatsAppEvidence(
                evidence_type=item.evidence_type,
                number=number,
                source_url=evidence_url,
                source=_clean_optional(item.source),
                official_source=item.official_source,
                context=_clean_optional(item.context),
                confirmed=confirmed,
                reason=reason,
            )
        )

    if not records_by_number:
        return WhatsAppResolution(
            status=WhatsAppStatus.NOT_FOUND,
            phone=normalized_phone,
            whatsapp=None,
            alternate_whatsapp_numbers=(),
            evidence=(),
            reason="No WhatsApp-specific number was found in the supplied evidence.",
        )

    ranked_numbers = sorted(
        records_by_number,
        key=lambda number: _candidate_rank(number, records_by_number[number]),
    )
    selected = ranked_numbers[0]
    selected_records = tuple(records_by_number[selected])
    status = (
        WhatsAppStatus.CONFIRMED
        if any(record.confirmed for record in selected_records)
        else WhatsAppStatus.UNCONFIRMED
    )
    reason = (
        "A public source explicitly identifies this number as WhatsApp."
        if status is WhatsAppStatus.CONFIRMED
        else "A WhatsApp candidate exists, but explicit public evidence is insufficient."
    )
    return WhatsAppResolution(
        status=status,
        phone=normalized_phone,
        whatsapp=selected,
        alternate_whatsapp_numbers=tuple(ranked_numbers[1:]),
        evidence=tuple(record for number in ranked_numbers for record in records_by_number[number]),
        reason=reason,
    )


def _remove_brazil_dialing_prefix(digits: str) -> str:
    if digits.startswith("0055") and len(digits) in {14, 15}:
        return digits[4:]
    if digits.startswith(BRAZIL_COUNTRY_CODE) and len(digits) in {12, 13}:
        return digits[2:]
    # Domestic 0 + DDD + number.
    if digits.startswith("0") and len(digits) in {11, 12}:
        return digits[1:]
    # Domestic 0 + two-digit carrier + DDD + number.
    if digits.startswith("0") and len(digits) in {13, 14}:
        return digits[3:]
    return digits


def _normalize_area_code(value: str | None) -> str | None:
    if not value:
        return None
    digits = "".join(character for character in value if character in "0123456789")
    if len(digits) == 3 and digits.startswith("0"):
        digits = digits[1:]
    return digits if digits in _VALID_AREA_CODES else None


def _normalize_public_http_url(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    candidate = html.unescape(value).strip()
    parsed = urlsplit(candidate)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None

    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        return None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if "." not in hostname:
            return None
    else:
        if not address.is_global:
            return None

    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port:
        netloc = f"{hostname}:{port}"
    return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path, parsed.query, ""))


def _direct_public_url(value: str) -> str | None:
    candidate = html.unescape(value).strip().strip("\"'<>.,;)")
    lowered = candidate.casefold()
    if lowered.startswith(("wa.me/", "api.whatsapp.com/", "web.whatsapp.com/")):
        candidate = f"https://{candidate}"
    parsed = urlsplit(candidate)
    hostname = (parsed.hostname or "").casefold().removeprefix("www.")
    if hostname not in {"wa.me", "api.whatsapp.com", "web.whatsapp.com", "whatsapp.com"}:
        return None
    return _normalize_public_http_url(candidate)


def _evaluate_evidence(
    item: WhatsAppEvidence,
    number: str,
    evidence_url: str | None,
    direct_phone: BrazilianPhone | None,
) -> tuple[bool, str]:
    if evidence_url is None:
        return False, "The WhatsApp claim has no valid public source URL."
    if not item.official_source:
        return False, "The WhatsApp claim was not published by an official company source."

    if item.evidence_type is WhatsAppEvidenceType.DIRECT_LINK:
        # The bounded website extractor stores the normalized number rather
        # than the raw href. Its typed record remains auditable through the
        # publishing page URL and can therefore be trusted here.
        if direct_phone is not None or parse_brazilian_phone(item.value) is not None:
            return True, "The official website publishes a WhatsApp contact link."
        return False, "The purported WhatsApp link does not carry a valid Brazilian number."

    if item.evidence_type is WhatsAppEvidenceType.STRUCTURED_FIELD:
        if _is_whatsapp_label(item.field_name):
            return True, "A public structured field explicitly named WhatsApp contains the number."
        return False, "The structured field is not explicitly identified as WhatsApp."

    if item.evidence_type is WhatsAppEvidenceType.EXPLICIT_LABEL:
        if _context_identifies_number(item.context, number):
            return True, "Public text explicitly labels this number as WhatsApp."
        return False, "The public text does not explicitly associate this number with WhatsApp."

    return False, "The source claims a candidate number without explicit public WhatsApp evidence."


def _is_whatsapp_label(value: str | None) -> bool:
    if not value:
        return False
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    normalized = re.sub(r"[^a-z0-9]+", " ", ascii_value.casefold()).strip()
    return bool(re.search(r"\bwhats\s*app\b", normalized))


def _context_identifies_number(context: str | None, number: str) -> bool:
    if not context or not _WHATSAPP_MARKER.search(context) or _NEGATIVE_WHATSAPP.search(context):
        return False
    return number in extract_brazilian_phone_numbers(context)


def _candidate_rank(
    number: str,
    records: list[ResolvedWhatsAppEvidence],
) -> tuple[int, int, int, str]:
    priority = {
        WhatsAppEvidenceType.DIRECT_LINK: 4,
        WhatsAppEvidenceType.STRUCTURED_FIELD: 3,
        WhatsAppEvidenceType.EXPLICIT_LABEL: 2,
        WhatsAppEvidenceType.CLAIMED_NUMBER: 1,
        WhatsAppEvidenceType.GENERIC_PHONE: 0,
    }
    return (
        -int(any(record.confirmed for record in records)),
        -max(priority[record.evidence_type] for record in records),
        -len(records),
        number,
    )


def _nearest_phone_to_marker(
    excerpt: str,
    *,
    marker_offset: int,
    default_area_code: str | None,
) -> str | None:
    candidates: list[tuple[int, int, str]] = []
    for match in _PHONE_LIKE.finditer(excerpt):
        number = normalize_brazilian_phone(match.group(0), default_area_code=default_area_code)
        if number is None:
            continue
        # Prefer a number after the label, then use textual distance.
        after_marker = int(match.start() >= marker_offset)
        distance = min(abs(match.start() - marker_offset), abs(match.end() - marker_offset))
        candidates.append((-after_marker, distance, number))
    if not candidates:
        return None
    return min(candidates)[2]


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


class _EvidenceHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.visible_parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "noscript"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        attributes = dict(attrs)
        href = attributes.get("href")
        if href:
            self.links.append(href)
        for attribute in ("aria-label", "title"):
            value = attributes.get(attribute)
            if value:
                self.visible_parts.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data.strip():
            self.visible_parts.append(data)
