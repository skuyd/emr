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
_V1_REFERENCE_KEYS = {
    "candidate_report_sha256",
    "hgnc_dataset_sha256",
    "hgnc_dataset_url",
    "hgnc_checked_on",
}
_V2_REFERENCE_KEYS = {
    "base_dictionary_sha256",
    "source_document",
    "source_document_sha256",
    "source_section",
}
_V1_INDICATOR_KEYS = {
    "aliases",
    "capability_level",
    "category",
    "code",
    "evidence_context_hashes",
    "ocr_variants",
    "standard_name",
    "unit_forms",
}
_V2_INDICATOR_KEYS = _V1_INDICATOR_KEYS | {"specimen", "result_types", "tier"}
_RESULT_TYPES = {"numeric", "comparator", "qualitative", "semi_quantitative", "status"}
_TIERS = {"TIER_1", "TIER_2", "GENE", "LEGACY"}
_CONTEXT = re.compile(r"[A-Z][A-Z0-9_]{1,63}\Z")


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
    specimen: str = ""
    result_types: tuple[str, ...] = ()
    tier: str = ""

    def __post_init__(self):
        if _CODE.fullmatch(self.code) is None:
            raise DictionaryError("invalid_indicator_code")
        if not isinstance(self.standard_name, str) or not 1 <= len(self.standard_name.strip()) <= 160:
            raise DictionaryError("invalid_standard_name")
        if _CATEGORY.fullmatch(self.category) is None:
            raise DictionaryError("invalid_indicator_category")
        if not self.aliases:
            raise DictionaryError("indicator_alias_required")
        if not self.evidence_context_hashes or any(
            _DIGEST.fullmatch(digest) is None for digest in self.evidence_context_hashes
        ):
            raise DictionaryError("invalid_evidence_context_hash")
        if self.specimen and _CONTEXT.fullmatch(self.specimen) is None:
            raise DictionaryError("invalid_specimen")
        if self.result_types and (
            len(self.result_types) != len(set(self.result_types))
            or any(result_type not in _RESULT_TYPES for result_type in self.result_types)
        ):
            raise DictionaryError("invalid_result_types")
        if self.tier and self.tier not in _TIERS:
            raise DictionaryError("invalid_tier")


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
                matches = aliases.setdefault(normalized, [])
                if any(previous.code == item.code for previous in matches):
                    continue
                if any(
                    not previous.specimen
                    or not previous.category
                    or not item.specimen
                    or not item.category
                    or (previous.specimen, previous.category) == (item.specimen, item.category)
                    for previous in matches
                ):
                    raise DictionaryError("ambiguous_indicator_alias")
                matches.append(item)
        object.__setattr__(self, "source_path", Path(self.source_path).resolve())
        object.__setattr__(
            self,
            "_alias_index",
            MappingProxyType({alias: tuple(matches) for alias, matches in aliases.items()}),
        )

    def match(self, raw_name, *, specimen="", panel=""):
        normalized = normalize_indicator_alias(raw_name)
        if not normalized:
            return None
        matches = self._alias_index.get(normalized, ())
        if specimen:
            normalized_specimen = normalize_indicator_alias(specimen)
            matches = tuple(
                item for item in matches if normalize_indicator_alias(item.specimen) == normalized_specimen
            )
        if panel:
            normalized_panel = normalize_indicator_alias(panel)
            matches = tuple(
                item for item in matches if normalize_indicator_alias(item.category) == normalized_panel
            )
        return matches[0] if len(matches) == 1 else None


def _load_indicator(value, *, schema_version):
    expected_keys = _V1_INDICATOR_KEYS if schema_version == "1.0" else _V2_INDICATOR_KEYS
    if not isinstance(value, dict) or set(value) != expected_keys:
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
    if schema_version == "2.0":
        result_types = _text_tuple(value["result_types"], field_name="result_types", allow_empty=False)
        specimen = value["specimen"]
        tier = value["tier"]
        if not isinstance(specimen, str) or not specimen:
            raise DictionaryError("invalid_specimen")
        if not isinstance(tier, str) or not tier:
            raise DictionaryError("invalid_tier")
    else:
        result_types = ()
        specimen = ""
        tier = ""
    return IndicatorDefinition(
        code=value["code"],
        standard_name=value["standard_name"],
        aliases=aliases,
        ocr_variants=ocr_variants,
        unit_forms=unit_forms,
        category=value["category"],
        capability_level=capability_level,
        evidence_context_hashes=evidence,
        specimen=specimen,
        result_types=result_types,
        tier=tier,
    )


def load_dictionary(path):
    path = Path(path)
    try:
        encoded = path.read_bytes()
    except OSError:
        raise DictionaryError("dictionary_unavailable") from None
    return load_dictionary_content(encoded, source_path=path)


def load_dictionary_content(encoded, *, source_path=_DEFAULT_RESOURCE):
    """Validate identical bytes from a bundled artifact or an immutable database release."""
    path = Path(source_path)
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (AttributeError, UnicodeError, json.JSONDecodeError):
        raise DictionaryError("dictionary_unavailable") from None
    if not isinstance(payload, dict) or set(payload) != _TOP_LEVEL_KEYS:
        raise DictionaryError("invalid_dictionary_schema")
    schema_version = payload["schema_version"]
    if schema_version not in {"1.0", "2.0"}:
        raise DictionaryError("invalid_dictionary_schema")
    version = payload["dictionary_version"]
    generated_from = payload["generated_from"]
    expected_reference_keys = _V1_REFERENCE_KEYS if schema_version == "1.0" else _V2_REFERENCE_KEYS
    if not isinstance(generated_from, dict) or set(generated_from) != expected_reference_keys:
        raise DictionaryError("invalid_dictionary_provenance")
    if any(
        not isinstance(generated_from[key], str) or not generated_from[key]
        for key in expected_reference_keys
    ):
        raise DictionaryError("invalid_dictionary_provenance")
    digest_keys = (
        ("candidate_report_sha256", "hgnc_dataset_sha256")
        if schema_version == "1.0"
        else ("base_dictionary_sha256", "source_document_sha256")
    )
    if any(_DIGEST.fullmatch(generated_from[key]) is None for key in digest_keys):
        raise DictionaryError("invalid_dictionary_provenance")
    indicators = payload["indicators"]
    if not isinstance(indicators, list):
        raise DictionaryError("invalid_dictionary_schema")
    try:
        loaded = tuple(_load_indicator(item, schema_version=schema_version) for item in indicators)
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


@lru_cache(maxsize=1)
def phase_two_dictionary():
    return load_dictionary(_DEFAULT_RESOURCE.parent / 'phase-two.json')


def current_dictionary():
    """Resolve the operator-published, deployment-bundled artifact for new runs."""

    from apps.operations.models import DictionaryRelease

    release = DictionaryRelease.objects.filter(active=True).first()
    if release is None:
        return phase_two_dictionary()
    return dictionary_for_release(release)


def rules_digest(rules):
    return hashlib.sha256(json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def release_digest(content_hash, rules):
    return hashlib.sha256(f"{content_hash}:{rules_digest(rules)}".encode("ascii")).hexdigest()


def dictionary_for_release(release):
    if (release.rules_hash and release.rules_hash != rules_digest(release.rules)
            or release.release_hash and release.release_hash != release_digest(release.content_hash, release.rules)
            or release.rules and not (release.rules_hash and release.release_hash)):
        raise DictionaryError("dictionary_rules_identity_mismatch")
    if release.payload:
        encoded = json.dumps(release.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        dictionary = load_dictionary_content(encoded, source_path=_DEFAULT_RESOURCE.parent / f"{release.version}.json")
    else:
        artifact = (_DEFAULT_RESOURCE.parent / release.artifact_name).resolve()
        try:
            artifact.relative_to(_DEFAULT_RESOURCE.parent.resolve())
        except ValueError:
            raise DictionaryError("dictionary_unavailable") from None
        dictionary = load_dictionary(artifact)
    if dictionary.version != release.version or dictionary.content_hash != release.content_hash:
        raise DictionaryError("dictionary_identity_mismatch")
    return dictionary


def dictionary_for_version(version):
    """Historical observations are checked against the definitions they actually used."""
    from apps.operations.models import DictionaryRelease

    release = DictionaryRelease.objects.filter(version=version).first()
    if release is not None:
        return dictionary_for_release(release)
    for dictionary in (default_dictionary(), phase_two_dictionary()):
        if dictionary.version == version:
            return dictionary
    raise DictionaryError("dictionary_version_unavailable")


def rules_for_version(version):
    from apps.operations.models import DictionaryRelease

    release = DictionaryRelease.objects.filter(version=version).first()
    if release is None:
        return ()
    dictionary_for_release(release)
    return tuple(release.rules)
