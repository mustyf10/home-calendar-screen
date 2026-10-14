from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

_PLATFORM_HOSTS = {
    "instagram.com": "instagram",
    "tiktok.com": "tiktok",
    "vm.tiktok.com": "tiktok",
    "youtube.com": "youtube",
    "m.youtube.com": "youtube",
    "youtu.be": "youtube",
}


def _host(url: str) -> str:
    host = (urlsplit(url.strip()).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = _host(url)
    netloc = host + (f":{parts.port}" if parts.port else "")
    query = ""
    if host in ("youtube.com", "m.youtube.com") and parts.path == "/watch":
        v = parse_qs(parts.query).get("v")
        if v:
            query = urlencode({"v": v[0]})
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def platform_for(url: str) -> str:
    return _PLATFORM_HOSTS.get(_host(url), "web")
