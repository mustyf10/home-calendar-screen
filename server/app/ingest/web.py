"""Fetch a web page and pull out recipe data: schema.org JSON-LD, og: tags, visible text."""
import copy
import html
import json
import math
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
PAGE_TEXT_LIMIT = 20_000


@dataclass
class PageData:
    url: str
    canonical_url: str
    og_title: str
    og_description: str
    og_image: str | None
    page_text: str
    jsonld_recipe: dict | None
    slides: list[str] = field(default_factory=list)  # TikTok photo post images, in order


def _client(timeout: float = 20) -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=timeout)


def _get_limited(url: str, max_bytes: int) -> tuple[bytes, httpx.Response]:
    """GET with a body-size cap; raises ValueError if the body is larger."""
    with _client() as client, client.stream("GET", url) as resp:
        resp.raise_for_status()
        chunks, size = [], 0
        for chunk in resp.iter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(f"Response larger than {max_bytes // (1024 * 1024)} MB")
            chunks.append(chunk)
        return b"".join(chunks), resp


def fetch_page(url: str) -> PageData:
    body, resp = _get_limited(url, MAX_PAGE_BYTES)
    # Without a charset header, hand BeautifulSoup the bytes so it can sniff <meta charset>.
    markup = body.decode(resp.charset_encoding, errors="replace") if resp.charset_encoding else body
    return parse_page(markup, str(resp.url))


def fetch_tiktok_page(url: str) -> PageData:
    """TikTok's /photo/ page is filled in by the browser; the same post under /video/ carries its data."""
    page = fetch_page(url)
    parts = urlsplit(page.url)
    if not page.slides and "/photo/" in parts.path:
        page = fetch_page(urlunsplit((parts.scheme, parts.netloc, parts.path.replace("/photo/", "/video/"), "", "")))
    return page


def download(url: str) -> bytes:
    return _get_limited(url, MAX_IMAGE_BYTES)[0]


def parse_page(markup: str | bytes, url: str) -> PageData:
    soup = BeautifulSoup(markup, "html.parser")
    recipe = extract_jsonld_recipe(soup)
    if recipe and recipe.get("image"):
        recipe["image"] = urljoin(url, recipe["image"])
    tiktok = _tiktok_item(soup)
    og_image = _meta(soup, "og:image") or _text_or_none((tiktok.get("video") or {}).get("cover"))
    canonical = soup.find("link", rel="canonical")
    canonical_url = urljoin(url, (canonical.get("href") if canonical else None) or _meta(soup, "og:url") or url)
    if urlsplit(canonical_url).scheme not in ("http", "https"):
        canonical_url = url
    image_post = tiktok.get("imagePost") if isinstance(tiktok.get("imagePost"), dict) else {}
    return PageData(
        url=url,
        canonical_url=canonical_url,
        og_title=_meta(soup, "og:title") or _text_or_none(image_post.get("title")) or "",
        og_description=_meta(soup, "og:description") or _text_or_none(tiktok.get("desc")) or "",
        og_image=urljoin(url, og_image) if og_image else None,
        page_text=_page_text(soup),
        jsonld_recipe=recipe,
        slides=_tiktok_slides(image_post),
    )


def _tiktok_item(soup: BeautifulSoup) -> dict:
    """TikTok video pages carry the caption and cover only in their rehydration JSON, not in meta tags."""
    script = soup.find("script", id="__UNIVERSAL_DATA_FOR_REHYDRATION__")
    try:
        data = json.loads(script.string or "") if script else None
        item = data["__DEFAULT_SCOPE__"]["webapp.video-detail"]["itemInfo"]["itemStruct"]
    except (json.JSONDecodeError, TypeError, KeyError):
        return {}
    return item if isinstance(item, dict) else {}


def _tiktok_slides(image_post: dict) -> list[str]:
    """First URL of each photo; TikTok lists mirrors of the same image after it."""
    slides = []
    images = image_post.get("images")
    for image in images if isinstance(images, list) else []:
        try:
            url = _text_or_none(image["imageURL"]["urlList"][0])
        except (KeyError, IndexError, TypeError):
            url = None
        if url:
            slides.append(url)
    return slides


def _text_or_none(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _meta(soup: BeautifulSoup, prop: str) -> str | None:
    tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    content = tag.get("content") if tag else None
    return content.strip() if content and content.strip() else None


def _page_text(soup: BeautifulSoup) -> str:
    body = copy.copy(soup)
    for tag in body(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    return " ".join(body.get_text(" ").split())[:PAGE_TEXT_LIMIT]


# --- JSON-LD -----------------------------------------------------------------

def extract_jsonld_recipe(soup: BeautifulSoup) -> dict | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        node = _find_recipe(data)
        if node is not None:
            return _normalise_recipe(node)
    return None


def _find_recipe(data) -> dict | None:
    if isinstance(data, list):
        for item in data:
            if (found := _find_recipe(item)) is not None:
                return found
        return None
    if not isinstance(data, dict):
        return None
    if "Recipe" in _types(data):
        return data
    if "@graph" in data:
        return _find_recipe(data["@graph"])
    return None


def _types(node: dict) -> list[str]:
    t = node.get("@type")
    return [t] if isinstance(t, str) else [x for x in t if isinstance(x, str)] if isinstance(t, list) else []


def _text(value) -> str:
    return " ".join(html.unescape(str(value)).split()) if value is not None else ""


def _normalise_recipe(node: dict) -> dict:
    return {
        "name": _text(node.get("name")),
        "description": _text(node.get("description")),
        "image": _first_image(node.get("image")),
        "ingredients": [s for s in (_text(i) for i in _as_list(node.get("recipeIngredient"))) if s],
        "steps": _steps(node.get("recipeInstructions")),
        "servings": _first_int(node.get("recipeYield")),
        "total_minutes": iso_minutes(node.get("totalTime")),
    }


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _first_image(value) -> str | None:
    for item in _as_list(value):
        if isinstance(item, str) and item.strip():
            return item.strip()
        if isinstance(item, dict):
            url = item.get("url") or item.get("contentUrl")
            if isinstance(url, str) and url.strip():
                return url.strip()
    return None


def _steps(value) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out += [s for s in (_text(line) for line in value.splitlines()) if s]
    elif isinstance(value, list):
        for item in value:
            out += _steps(item)
    elif isinstance(value, dict):
        if "HowToSection" in _types(value) or "itemListElement" in value:
            out += _steps(value.get("itemListElement"))
        else:
            text = _text(value.get("text") or value.get("name"))
            if text:
                out.append(text)
    return out


def _first_int(value) -> int | None:
    for item in _as_list(value):
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            return item
        m = re.search(r"\d+", str(item))
        if m:
            return int(m.group())
    return None


_ISO_DURATION = re.compile(
    r"^P(?:(?P<d>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<h>\d+(?:\.\d+)?)H)?(?:(?P<m>\d+(?:\.\d+)?)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$"
)


def iso_minutes(value) -> int | None:
    """ISO-8601 duration ('PT1H15M') → whole minutes, rounded up; None if unparseable."""
    if not isinstance(value, str):
        return None
    m = _ISO_DURATION.match(value.strip().upper())
    if not m or not any(m.groupdict().values()):
        return None
    parts = {k: float(v) if v else 0.0 for k, v in m.groupdict().items()}
    minutes = parts["d"] * 1440 + parts["h"] * 60 + parts["m"] + parts["s"] / 60
    return math.ceil(minutes) if minutes else None
