"""Urban API roots for direct connections and load-balancer mounts."""

from urllib.parse import urlsplit, urlunsplit


def normalize_urban_api_url(base_url: str) -> str:
    """Validate the configured API root without inventing a proxy path."""
    url = urlsplit(base_url.strip())
    if (
        url.scheme not in {"http", "https"}
        or not url.netloc
        or url.query
        or url.fragment
    ):
        raise ValueError(
            "Urban API URL must be an HTTP(S) base URL without query or fragment"
        )
    path = url.path.rstrip("/")
    return urlunsplit((url.scheme, url.netloc, path, "", ""))
