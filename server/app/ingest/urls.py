from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

_PLATFORM_HOSTS = {
    "instagram.com": "instagram",
    "tiktok.com": "tiktok",
    "vm.tiktok.com": "tiktok",
    "youtube.com": "youtube",
    "m.youtube.com": "youtube",
    "youtu.be": "youtube",
}


def host(url: str) -> str:
    """Lowercase hostname without a leading www."""
    name = (urlsplit(url.strip()).hostname or "").lower()
    return name[4:] if name.startswith("www.") else name


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    netloc = host(url) + (f":{parts.port}" if parts.port else "")
    query = ""
    if host(url) in ("youtube.com", "m.youtube.com") and parts.path == "/watch":
        v = parse_qs(parts.query).get("v")
        if v:
            query = urlencode({"v": v[0]})
    path = parts.path.rstrip("/")
    scheme = "https" if parts.scheme.lower() in ("http", "https") else parts.scheme.lower()
    return urlunsplit((scheme, netloc, path, query, ""))


def platform_for(url: str) -> str:
    return _PLATFORM_HOSTS.get(host(url), "web")
