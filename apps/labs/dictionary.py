from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
import unicodedata


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}\Z")
_CATEGORY = re.compile(r"[A-Z][A-Z0-9_]{2,63}\Z")
_DEFAULT_RESOURCE = Path(__file__).resolve().parent / "dictionaries" / "v1.0.0.json"
_TOP_LEVEL_KEYS = {"schema_version", "dictionary_version", "generated_from", "indicators"}
_REFERENCE_KEYS = {
    "candidate_report_sha256",
    "hgnc_dataset_sha256",
    "hgnc_dataset_url",
    "hgnc_checked_on",
}
_INDICATOR_KEYS = {
    "aliases",
    "capability_level",
    "category",
    "code",
    "evidence_context_hashes",
    "ocr_variants",
    "standard_name",
    "unit_forms",
}


class DictionaryError(ValueError):
    pass


class CapabilityLevel(str, Enum):
    STABLE = "STABLE"
    EXPLORATORY = "EXPLORATORY"
    SEARCH_ONLY = "SEARCH_ONLY"


def normalize_indicator_alias(value):
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value)
    value = "".join(character for character in value if not unicodedata.category(character).startswith("C"))
    value = re.sub(r"[★☆△*]+", "", value)
    value = re.sub(r"\s+", " ", value).strip(" :：,，;；|｜")
    value = re.sub(r"(?<=\w)\s+(?=\w)", "", value)
    return value.casefold()


def _text_tuple(value, *, field_name, allow_empty=True):
    if not isinstance(value, list) or (not allow_empty and not value):
        raise DictionaryError(f"invalid_{field_name}")
    items = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > 160:
            raise DictionaryError(f"invalid_{field_name}")
        item = item.strip()
        if item not in items:
            items.append(item)
    return tuple(items)


@dataclass(frozen=True)
class IndicatorDefinition:
    code: str
    standard_name: str
    aliases: tuple[str, ...]
    ocr_variants: tuple[str, ...]
    unit_forms: tuple[str, ...]
    category: str
    capability_level: CapabilityLevel
    evidence_context_hashes: tuple[str, ...]

    def __post_init__(self):
        if _CODE.fullmatch(self.code) is None:
            raise DictionaryError("invalid_indicator_code")
        if not isinstance(self.standard_name, str) or not 2 <= len(self.standard_name.strip()) <= 160:
            raise DictionaryError("invalid_standard_name")
        if _CATEGORY.fullmatch(self.category) is None:
            raise DictionaryError("invalid_indicator_category")
        if not self.aliases:
            raise DictionaryError("indicator_alias_required")
        if not self.evidence_context_hashes or any(
            _DIGEST.fullmatch(digest) is None for digest in self.evidence_context_hashes
        ):
            raise DictionaryError("invalid_evidence_context_hash")


@dataclass(frozen=True)
class IndicatorDictionary:
    version: str
    content_hash: str
    indicators: tuple[IndicatorDefinition, ...]
    source_path: Path
    generated_from: tuple[tuple[str, str], ...]
    _alias_index: MappingProxyType = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if _VERSION.fullmatch(self.version) is None or _DIGEST.fullmatch(self.content_hash) is None:
            raise DictionaryError("invalid_dictionary_identity")
        if not self.indicators:
            raise DictionaryError("empty_indicator_dictionary")
        codes = [item.code for item in self.indicators]
        names = [item.standard_name for item in self.indicators]
        if len(codes) != len(set(codes)) or len(names) != len(set(names)):
            raise DictionaryError("duplicate_indicator_identity")
        aliases = {}
        for item in self.indicators:
            for raw_alias in (item.standard_name, *item.aliases, *item.ocr_variants):
                normalized = normalize_indicator_alias(raw_alias)
                if not normalized:
                    raise DictionaryError("invalid_indicator_alias")
                previous = aliases.get(normalized)
                if previous is not None and previous.code != item.code:
                    raise DictionaryError("ambiguous_indicator_alias")
                aliases[normalized] = item
        object.__setattr__(self, "source_path", Path(self.source_path).resolve())
        object.__setattr__(self, "_alias_index", MappingProxyType(aliases))

    def match(self, raw_name):
        normalized = normalize_indicator_alias(raw_name)
        return self._alias_index.get(normalized) if normalized else None


def _load_indicator(value):
    if not isinstance(value, dict) or set(value) != _INDICATOR_KEYS:
        raise DictionaryError("invalid_indicator_record")
    try:
        capability_level = CapabilityLevel(value["capability_level"])
    except (TypeError, ValueError):
        raise DictionaryError("invalid_capability_level") from None
    aliases = _text_tuple(value["aliases"], field_name="aliases", allow_empty=False)
    ocr_variants = _text_tuple(value["ocr_variants"], field_name="ocr_variants")
    unit_forms = _text_tuple(value["unit_forms"], field_name="unit_forms")
    evidence = _text_tuple(
        value["evidence_context_hashes"],
        field_name="evidence_context_hashes",
        allow_empty=False,
    )
    return IndicatorDefinition(
        code=value["code"],
        standard_name=value["standard_name"],
        aliases=aliases,
        ocr_variants=ocr_variants,
        unit_forms=unit_forms,
        category=value["category"],
        capability_level=capability_level,
        evidence_context_hashes=evidence,
    )


def load_dictionary(path):
    path = Path(path)
    try:
        encoded = path.read_bytes()
        payload = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise DictionaryError("dictionary_unavailable") from None
    if not isinstance(payload, dict) or set(payload) != _TOP_LEVEL_KEYS or payload["schema_version"] != "1.0":
        raise DictionaryError("invalid_dictionary_schema")
    version = payload["dictionary_version"]
    generated_from = payload["generated_from"]
    if not isinstance(generated_from, dict) or set(generated_from) != _REFERENCE_KEYS:
        raise DictionaryError("invalid_dictionary_provenance")
    if any(
        not isinstance(generated_from[key], str) or not generated_from[key]
        for key in _REFERENCE_KEYS
    ):
        raise DictionaryError("invalid_dictionary_provenance")
    if _DIGEST.fullmatch(generated_from["candidate_report_sha256"]) is None or _DIGEST.fullmatch(
        generated_from["hgnc_dataset_sha256"]
    ) is None:
        raise DictionaryError("invalid_dictionary_provenance")
    indicators = payload["indicators"]
    if not isinstance(indicators, list):
        raise DictionaryError("invalid_dictionary_schema")
    try:
        loaded = tuple(_load_indicator(item) for item in indicators)
    except (KeyError, TypeError):
        raise DictionaryError("invalid_indicator_record") from None
    return IndicatorDictionary(
        version=version,
        content_hash=hashlib.sha256(encoded).hexdigest(),
        indicators=loaded,
        source_path=path,
        generated_from=tuple(sorted(generated_from.items())),
    )


@lru_cache(maxsize=1)
def default_dictionary():
    return load_dictionary(_DEFAULT_RESOURCE)


def current_dictionary():
    """Resolve the operator-published, deployment-bundled artifact for new runs."""

    from apps.operations.models import DictionaryRelease

    release = DictionaryRelease.objects.filter(active=True).only("artifact_name").first()
    if release is None:
        return default_dictionary()
    artifact = (_DEFAULT_RESOURCE.parent / release.artifact_name).resolve()
    try:
        artifact.relative_to(_DEFAULT_RESOURCE.parent.resolve())
    except ValueError:
        raise DictionaryError("dictionary_unavailable") from None
    dictionary = load_dictionary(artifact)
    if dictionary.version != release.version or dictionary.content_hash != release.content_hash:
        raise DictionaryError("dictionary_identity_mismatch")
    return dictionary
