from dataclasses import dataclass
import json
from urllib.parse import quote, quote_plus


@dataclass(frozen=True)
class SyntheticCanary:
    label: str
    value: str


@dataclass(frozen=True)
class PrivacyLeak:
    artifact: str
    canary: str
    representation: str


def _render_artifact(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _representations(value):
    escaped = json.dumps(value, ensure_ascii=True)[1:-1]
    return {
        "plain": value,
        "url": quote(value, safe=""),
        "form": quote_plus(value, safe=""),
        "json_escape": escaped,
    }


def scan_runtime_artifacts(artifacts, canaries):
    """Find synthetic privacy canaries in non-source runtime artifacts.

    Callers deliberately choose the artifact boundary. Authorized source
    records and authorized HTML responses should not be included.
    """

    leaks = []
    rendered = {name: _render_artifact(value) for name, value in artifacts.items()}
    for canary in canaries:
        if not isinstance(canary, SyntheticCanary) or not canary.label or not canary.value:
            raise ValueError("Canaries require non-empty labels and values")
        for representation, needle in _representations(canary.value).items():
            if not needle:
                continue
            for artifact, haystack in rendered.items():
                if needle in haystack:
                    leaks.append(PrivacyLeak(artifact, canary.label, representation))
    return tuple(leaks)
