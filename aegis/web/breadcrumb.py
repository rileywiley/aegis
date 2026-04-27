"""Dynamic breadcrumb helper — resolves back navigation from ?from= param, referer, or default."""

from urllib.parse import urlparse

from starlette.requests import Request

# Label mapping for URL paths
_PATH_LABELS: dict[str, str] = {
    "/": "Command Center",
    "/meetings": "Meetings",
    "/emails": "Emails",
    "/asks": "Asks",
    "/workstreams": "Workstreams",
    "/departments": "Departments",
    "/search": "Search Results",
    "/readiness": "Readiness",
    "/people": "People",
    "/actions": "Actions",
    "/org": "Org Chart",
    "/respond": "Response",
    "/admin": "Admin",
    "/ask": "Ask Aegis",
}


def resolve_breadcrumb(
    request: Request,
    from_param: str | None,
    default_url: str,
    default_label: str,
) -> tuple[str, str]:
    """Return (back_url, back_label) from ?from= param, referer, or default."""
    # 1. Check ?from= query parameter
    if from_param:
        label = _get_label_for_path(from_param)
        return from_param, label

    # 2. Fall back to HTTP Referer (extract path only, ignore external)
    referer = request.headers.get("referer", "")
    if referer:
        parsed = urlparse(referer)
        request_host = request.url.netloc if hasattr(request.url, "netloc") else ""
        safe_hosts = ("", "localhost:8000", "127.0.0.1:8000", request_host)
        if parsed.netloc in safe_hosts:
            path = parsed.path
            if path and path != str(request.url.path):  # Don't link back to self
                label = _get_label_for_path(path)
                full_url = path + ("?" + parsed.query if parsed.query else "")
                return full_url, label

    # 3. Default
    return default_url, default_label


def _get_label_for_path(path: str) -> str:
    """Get human-readable label for a URL path."""
    # Strip query string if accidentally included
    clean_path = path.split("?")[0]

    # Exact match first
    if clean_path in _PATH_LABELS:
        return _PATH_LABELS[clean_path]

    # Check prefix (e.g., /workstreams/123 -> "Workstream")
    for prefix, label in _PATH_LABELS.items():
        if prefix != "/" and clean_path.startswith(prefix + "/"):
            # Singular form for detail pages
            return label.rstrip("s") if label.endswith("s") else label

    return "Back"
