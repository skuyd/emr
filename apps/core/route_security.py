from dataclasses import dataclass
import inspect

from django.urls import URLPattern, URLResolver, get_resolver


@dataclass(frozen=True)
class ApplicationRoute:
    name: str
    route: str
    methods: tuple[str, ...]
    patient_scoped: bool
    csrf_exempt: bool


def _callback_chain(callback):
    seen = set()
    current = callback
    while callable(current) and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = getattr(current, "__wrapped__", None)


def _declared_methods(callback):
    for candidate in _callback_chain(callback):
        try:
            nonlocals = inspect.getclosurevars(candidate).nonlocals
        except (TypeError, ValueError):
            continue
        methods = nonlocals.get("request_method_list")
        if methods:
            return tuple(str(method).upper() for method in methods)
    return ()


def _qualified_name(namespaces, pattern):
    parts = [*namespaces]
    if pattern.name:
        parts.append(pattern.name)
    return ":".join(parts)


def _walk(patterns, *, prefix="", namespaces=()):
    for entry in patterns:
        route = f"{prefix}{entry.pattern}"
        if isinstance(entry, URLResolver):
            child_namespaces = namespaces + ((entry.namespace,) if entry.namespace else ())
            yield from _walk(entry.url_patterns, prefix=route, namespaces=child_namespaces)
            continue
        if not isinstance(entry, URLPattern):
            continue
        chain = tuple(_callback_chain(entry.callback))
        yield ApplicationRoute(
            name=_qualified_name(namespaces, entry),
            route=route,
            methods=_declared_methods(entry.callback),
            patient_scoped=any(getattr(callback, "patient_scoped", False) for callback in chain),
            csrf_exempt=any(getattr(callback, "csrf_exempt", False) for callback in chain),
        )


def application_routes():
    """Return a stable security inventory for every configured URL pattern."""

    return tuple(_walk(get_resolver().url_patterns))
