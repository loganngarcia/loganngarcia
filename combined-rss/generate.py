#!/usr/bin/env python3
import hashlib
import html as html_lib
import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

OUT = Path(__file__).with_name("feed.xml")
SELF = "https://raw.githubusercontent.com/loganngarcia/loganngarcia/main/combined-rss/feed.xml"
TECHMEME_LOCAL = Path(__file__).resolve().parents[1] / "techmeme-oneclick" / "feed.xml"

SOURCES = {
    "Humanoids Daily": "https://www.humanoidsdaily.com/feed.xml",
    "Sherwood News": "https://sherwood.news/rss.xml",
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})

MAX_PER_SOURCE = {
    "Techmeme": 60,
    "Humanoids Daily": 35,
    "Sherwood News": 35,
    "The AI Timeline": 25,
    "Qwen Research": 35,
}
MAX_OUTPUT = 170

BAD_IMAGE_WORDS = (
    "favicon", "logo", "avatar", "author", "profile", "sprite", "tracking",
    "pixel", "badge", "emoji", "icon-", "/icon", "1x1", "spacer",
)


def clean_text(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", html_lib.unescape(value)).strip()


def short_desc(value, limit=420):
    value = clean_text(value)
    if len(value) <= limit:
        return value
    cut = value[: limit + 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut + "…"


def parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = dateparser.parse(str(value))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def rfc822(dt):
    dt = dt or datetime.now(timezone.utc)
    return format_datetime(dt.astimezone(timezone.utc))


def absolute(base, url):
    if not url:
        return ""
    return urllib.parse.urljoin(base, html_lib.unescape(url.strip()))


def good_image(url):
    if not url or not re.match(r"^https?://", url, re.I):
        return False
    low = urllib.parse.urlparse(url).path.lower()
    return not any(word in low for word in BAD_IMAGE_WORDS)


def first_image_from_html(fragment, base=""):
    if not fragment:
        return ""
    soup = BeautifulSoup(fragment, "html.parser")
    for img in soup.find_all("img"):
        url = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        url = absolute(base, url)
        if good_image(url):
            return url
    return ""


def pick_feed_image(entry, base=""):
    candidates = []
    for obj in getattr(entry, "media_content", []) or []:
        candidates.append(obj.get("url"))
    for obj in getattr(entry, "media_thumbnail", []) or []:
        candidates.append(obj.get("url"))
    for obj in getattr(entry, "enclosures", []) or []:
        typ = (obj.get("type") or "").lower()
        if typ.startswith("image/") or good_image(obj.get("href") or obj.get("url") or ""):
            candidates.append(obj.get("href") or obj.get("url"))
    for content in getattr(entry, "content", []) or []:
        candidates.append(first_image_from_html(content.get("value", ""), base))
    candidates.append(first_image_from_html(getattr(entry, "summary", ""), base))
    for url in candidates:
        url = absolute(base, url or "")
        if good_image(url):
            return url
    return ""


def get_html(url, timeout=12):
    headers_list = [
        {},
        {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"},
        {"User-Agent": "Googlebot/2.1 (+http://www.google.com/bot.html)"},
    ]
    last = None
    for extra in headers_list:
        try:
            r = SESSION.get(url, headers=extra, timeout=timeout, allow_redirects=True)
            last = r
            ctype = (r.headers.get("content-type") or "").lower()
            if r.ok and ("html" in ctype or not ctype) and len(r.text) > 200:
                return r.text, r.url
        except requests.RequestException:
            pass
    if last is not None and last.ok and len(last.text) > 200:
        return last.text, last.url
    return "", url


def jsonld_objects(soup):
    out = []
    for script in soup.find_all("script", type=lambda x: x and "ld+json" in x.lower()):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            obj = stack.pop(0)
            if isinstance(obj, dict):
                out.append(obj)
                graph = obj.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
            elif isinstance(obj, list):
                stack.extend(obj)
    return out


def image_from_jsonld(value, base):
    if isinstance(value, str):
        return absolute(base, value)
    if isinstance(value, list):
        for v in value:
            got = image_from_jsonld(v, base)
            if good_image(got):
                return got
    if isinstance(value, dict):
        for key in ("url", "contentUrl", "thumbnailUrl"):
            if value.get(key):
                return absolute(base, value[key])
    return ""


def extract_direct_metadata(url):
    html, final_url = get_html(url)
    if not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")

    def meta(*keys):
        for key in keys:
            tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
            if tag and tag.get("content"):
                return clean_text(tag.get("content"))
        return ""

    title = meta("og:title", "twitter:title")
    desc = meta("og:description", "twitter:description", "description")
    image = meta("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src")
    image = absolute(final_url, image)
    published = meta("article:published_time", "date", "datePublished")

    article_types = {"article", "newsarticle", "blogposting", "report", "scholarlyarticle"}
    for obj in jsonld_objects(soup):
        typ = obj.get("@type", "")
        types = [typ] if isinstance(typ, str) else typ if isinstance(typ, list) else []
        if not any(str(t).lower() in article_types for t in types):
            continue
        title = title or clean_text(obj.get("headline") or obj.get("name"))
        desc = desc or clean_text(obj.get("description"))
        if not good_image(image):
            image = image_from_jsonld(obj.get("image") or obj.get("thumbnailUrl"), final_url)
        published = published or obj.get("datePublished") or obj.get("dateCreated")

    if not title:
        h1 = soup.find("h1")
        title = clean_text(h1.get_text(" ", strip=True)) if h1 else ""
    if not title and soup.title:
        title = clean_text(soup.title.get_text(" ", strip=True))

    if not desc:
        article = soup.find("article") or soup.find("main")
        if article:
            for p in article.find_all("p"):
                text = clean_text(p.get_text(" ", strip=True))
                if len(text) >= 80:
                    desc = text
                    break

    if not good_image(image):
        scope = soup.find("article") or soup.find("main") or soup
        for img in scope.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or img.get("srcset")
            if src and "," in src:
                src = src.split(",")[-1].strip().split(" ")[0]
            candidate = absolute(final_url, src or "")
            if good_image(candidate):
                image = candidate
                break

    return {
        "title": clean_text(title),
        "description": short_desc(desc),
        "image": image if good_image(image) else "",
        "published": parse_dt(published),
        "final_url": final_url,
    }


def extract_cardyb(url):
    try:
        endpoint = "https://cardyb.bsky.app/v1/extract?" + urllib.parse.urlencode({"url": url})
        r = SESSION.get(endpoint, timeout=15)
        if not r.ok:
            return {}
        data = r.json() or {}
        if data.get("error"):
            return {}
        image = data.get("image") or ""
        return {
            "title": clean_text(data.get("title")),
            "description": short_desc(data.get("description")),
            "image": image if good_image(image) else "",
            "published": None,
        }
    except Exception:
        return {}


def extract_linkmetadata(url):
    try:
        endpoint = "https://api.linkmetadata.com/v1/metadata?" + urllib.parse.urlencode({"url": url})
        r = SESSION.get(endpoint, timeout=15)
        if not r.ok:
            return {}
        data = r.json() or {}
        image = data.get("image") or data.get("image_url") or ""
        if isinstance(image, dict):
            image = image.get("url") or image.get("src") or ""
        return {
            "title": clean_text(data.get("title")),
            "description": short_desc(data.get("description")),
            "image": image if good_image(image) else "",
            "published": parse_dt(data.get("published_at") or data.get("date")),
        }
    except Exception:
        return {}


def extract_ogfetch(url):
    try:
        endpoint = "https://api.ogfetch.com/preview?" + urllib.parse.urlencode({"url": url})
        r = SESSION.get(endpoint, timeout=15)
        if not r.ok:
            return {}
        data = r.json() or {}
        image = data.get("image") or ""
        if isinstance(image, dict):
            image = image.get("url") or image.get("src") or ""
        return {
            "title": clean_text(data.get("title")),
            "description": short_desc(data.get("description")),
            "image": image if good_image(image) else "",
            "published": parse_dt(data.get("published_at") or data.get("date")),
        }
    except Exception:
        return {}


def extract_microlink(url):
    try:
        endpoint = "https://api.microlink.io/?" + urllib.parse.urlencode({"url": url, "meta": "true"})
        r = SESSION.get(endpoint, timeout=15)
        if not r.ok:
            return {}
        data = (r.json() or {}).get("data") or {}
        image_obj = data.get("image")
        image = (image_obj.get("url") if isinstance(image_obj, dict) else image_obj) or ""
        return {
            "title": clean_text(data.get("title")),
            "description": short_desc(data.get("description")),
            "image": image if good_image(image) else "",
            "published": parse_dt(data.get("date")),
        }
    except Exception:
        return {}


def extract_jina(url):
    try:
        endpoint = "https://r.jina.ai/" + url
        r = SESSION.get(endpoint, headers={"Accept": "text/plain"}, timeout=20)
        if not r.ok or len(r.text) < 100:
            return {}
        text = r.text
        title = ""
        m = re.search(r"(?mi)^Title:\s*(.+)$", text)
        if m:
            title = clean_text(m.group(1))
        published = ""
        m = re.search(r"(?mi)^Published Time:\s*(.+)$", text)
        if m:
            published = m.group(1).strip()
        image = ""
        for match in re.finditer(r"!\[[^\]]*\]\((https?://[^\s)]+)", text):
            candidate = html_lib.unescape(match.group(1))
            if good_image(candidate):
                image = candidate
                break
        desc = ""
        body = text.split("Markdown Content:", 1)[-1]
        for block in re.split(r"\n\s*\n", body):
            block = clean_text(re.sub(r"^[#>*\-\s]+", "", block))
            if len(block) >= 90 and not block.lower().startswith(("image", "cookie", "subscribe", "sign in")):
                desc = block
                break
        return {
            "title": title,
            "description": short_desc(desc),
            "image": image,
            "published": parse_dt(published),
        }
    except Exception:
        return {}


def enrich(item, cached=None, prefer_page_title=False):
    cached = cached or {}
    if cached.get("description"):
        item["description"] = cached["description"]
    if cached.get("image"):
        item["image"] = cached["image"]
    if cached.get("published") and not item.get("published"):
        item["published"] = cached["published"]

    need_desc = not item.get("description") or item["description"].lower().startswith("from ")
    need_image = not good_image(item.get("image", ""))
    need_date = not item.get("published")
    need_title = prefer_page_title and (not item.get("title") or len(item.get("title", "")) > 220)

    if not (need_desc or need_image or need_date or need_title):
        return item

    page = extract_direct_metadata(item["link"])
    if page:
        if prefer_page_title and page.get("title"):
            item["title"] = page["title"]
        if page.get("description"):
            item["description"] = page["description"]
        if page.get("image"):
            item["image"] = page["image"]
        if page.get("published"):
            item["published"] = item.get("published") or page["published"]

    need_desc = not item.get("description") or item["description"].lower().startswith("from ")
    need_image = not good_image(item.get("image", ""))
    need_date = not item.get("published")

    for extractor in (extract_cardyb, extract_linkmetadata, extract_ogfetch):
        if not (need_desc or need_image or need_date):
            break
        meta = extractor(item["link"])
        if meta.get("description") and need_desc:
            item["description"] = meta["description"]
        if meta.get("image") and need_image:
            item["image"] = meta["image"]
        if meta.get("published") and need_date:
            item["published"] = meta["published"]
        if prefer_page_title and meta.get("title") and not item.get("title"):
            item["title"] = meta["title"]
        need_desc = not item.get("description") or item["description"].lower().startswith("from ")
        need_image = not good_image(item.get("image", ""))
        need_date = not item.get("published")

    if need_desc or need_image or need_date:
        meta = extract_microlink(item["link"])
        if meta.get("description") and need_desc:
            item["description"] = meta["description"]
        if meta.get("image") and need_image:
            item["image"] = meta["image"]
        if meta.get("published") and need_date:
            item["published"] = meta["published"]
        if prefer_page_title and meta.get("title") and not item.get("title"):
            item["title"] = meta["title"]

    need_desc = not item.get("description") or item["description"].lower().startswith("from ")
    need_image = not good_image(item.get("image", ""))
    need_date = not item.get("published")
    if need_desc or need_image or need_date:
        meta = extract_jina(item["link"])
        if meta.get("description") and need_desc:
            item["description"] = meta["description"]
        if meta.get("image") and need_image:
            item["image"] = meta["image"]
        if meta.get("published") and need_date:
            item["published"] = meta["published"]
        if prefer_page_title and meta.get("title") and not item.get("title"):
            item["title"] = meta["title"]

    if not item.get("description") and item.get("source") == "Techmeme":
        summary = re.sub(r"\\s*\\([^()]{1,120}\\)\\s*$", "", item.get("title", "")).strip()
        item["description"] = short_desc(summary)
    item["description"] = short_desc(item.get("description") or "")
    if not good_image(item.get("image", "")):
        item["image"] = ""
    return item


def make_guid(source, link):
    return "combined:" + hashlib.sha1((source + "\n" + link).encode()).hexdigest()


def load_cached():
    if not OUT.exists():
        return {}
    try:
        root = ET.parse(OUT).getroot()
    except Exception:
        return {}
    ns = {"media": "http://search.yahoo.com/mrss/"}
    cache = {}
    for node in root.findall("./channel/item"):
        link = node.findtext("link") or ""
        source_node = node.find("source")
        source = source_node.text.strip() if source_node is not None and source_node.text else ""
        desc = clean_text(node.findtext("description") or "")
        img_node = node.find("media:content", ns)
        if img_node is None:
            img_node = node.find("media:thumbnail", ns)
        image = img_node.get("url") if img_node is not None else ""
        pub = parse_dt(node.findtext("pubDate"))
        if link:
            cache[(source, link)] = {"description": desc, "image": image, "published": pub}
    return cache


def feed_items(url, source):
    try:
        r = SESSION.get(url, timeout=20)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as e:
        print(f"WARN {source} feed failed: {e}")
        return []
    out = []
    for entry in feed.entries[: MAX_PER_SOURCE[source]]:
        link = entry.get("link") or ""
        title = clean_text(entry.get("title"))
        if not link or not title:
            continue
        desc = entry.get("summary") or entry.get("description") or ""
        if not desc and entry.get("content"):
            desc = entry.content[0].get("value", "")
        published = parse_dt(entry.get("published") or entry.get("updated") or entry.get("date"))
        out.append({
            "source": source,
            "title": title,
            "link": link,
            "description": short_desc(desc),
            "image": pick_feed_image(entry, link),
            "published": published,
            "guid": entry.get("id") or entry.get("guid") or make_guid(source, link),
        })
    return out


def techmeme_items():
    if not TECHMEME_LOCAL.exists():
        return []
    feed = feedparser.parse(str(TECHMEME_LOCAL))
    out = []
    for entry in feed.entries[: MAX_PER_SOURCE["Techmeme"]]:
        link = entry.get("link") or ""
        title = clean_text(entry.get("title"))
        if not link or not title:
            continue
        desc = entry.get("summary") or entry.get("description") or ""
        out.append({
            "source": "Techmeme",
            "title": title,
            "link": link,
            "description": short_desc(desc) if not clean_text(desc).lower().startswith("from ") else "",
            "image": pick_feed_image(entry, link),
            "published": parse_dt(entry.get("published") or entry.get("updated")),
            "guid": entry.get("id") or entry.get("guid") or make_guid("Techmeme", link),
        })
    return out


def discover_bicloud():
    pages = [
        "https://mail.bycloud.ai/",
        "https://mail.bycloud.ai/archive",
        "https://mail.bycloud.ai/archive?page=2",
    ]
    found = {}
    for page in pages:
        html, final = get_html(page)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = absolute(final, a["href"])
            if not href.startswith("https://mail.bycloud.ai/p/"):
                continue
            title = clean_text(a.get_text(" ", strip=True))
            if href not in found or (title and (not found[href].get("title") or len(title) < len(found[href]["title"]))):
                found[href] = {"title": title}
    return [{
        "source": "The AI Timeline",
        "title": data.get("title", ""),
        "link": link,
        "description": "",
        "image": "",
        "published": None,
        "guid": make_guid("The AI Timeline", link),
    } for link, data in list(found.items())[: MAX_PER_SOURCE["The AI Timeline"]]]


def discover_qwen():
    found = {}

    def add_candidate(raw, title=""):
        if not raw:
            return
        raw = html_lib.unescape(str(raw))
        raw = (
            raw.replace("\\u0026", "&")
               .replace("\\u003f", "?")
               .replace("\\u003d", "=")
               .replace("\\/", "/")
               .strip()
        )
        raw = raw.rstrip(").,;:'\"\\")
        link = absolute("https://qwen.ai", raw)
        parsed = urllib.parse.urlparse(link)
        if parsed.hostname not in {"qwen.ai", "www.qwen.ai"}:
            return
        is_blog = parsed.path.rstrip("/") == "/blog" and "id=" in parsed.query
        is_research = parsed.path.startswith("/research/") and parsed.path.rstrip("/") != "/research"
        if not (is_blog or is_research):
            return
        # Drop locale/tracking-only noise while preserving the ID and research-origin
        # parameters that identify Qwen posts.
        found.setdefault(link, {"title": clean_text(title)})
        if title and not found[link].get("title"):
            found[link]["title"] = clean_text(title)

    direct_pages = [
        ("https://qwen.ai/research", "https://qwen.ai/research"),
        ("https://qwen.ai/home", "https://qwen.ai/home"),
        ("https://qwen.ai/sitemap.xml", "https://qwen.ai/sitemap.xml"),
    ]

    for page, base in direct_pages:
        try:
            r = SESSION.get(page, timeout=20)
            if not r.ok:
                continue
            text = r.text
        except requests.RequestException:
            continue

        if "xml" in (r.headers.get("content-type") or "").lower() or text.lstrip().startswith("<?xml"):
            try:
                root = ET.fromstring(text)
                for loc in root.findall(".//{*}loc"):
                    add_candidate(clean_text(loc.text))
            except Exception:
                pass
        else:
            soup = BeautifulSoup(text, "html.parser")
            if page in {"https://qwen.ai/research", "https://qwen.ai/home"}:
                scripts = [absolute(page, tag.get("src")) for tag in soup.find_all("script", src=True)]
                print(f"QWEN DEBUG {page} html_len={len(text)} scripts={scripts[:30]}")
                if page == "https://qwen.ai/research":
                    scripts = ["https://g.alicdn.com/qwenweb/qwen-ai-fe/0.0.79/js/p_research-index.js"] + scripts
                    for src in scripts[:31]:
                        try:
                            jsr = SESSION.get(src, timeout=15)
                            if not jsr.ok:
                                continue
                            js = jsr.text
                        except requests.RequestException:
                            continue
                        hits = []
                        for keyword in ("research-list", "latest-advancements", "researchList", "blogList", "publication"):
                            pos2 = js.find(keyword)
                            if pos2 >= 0:
                                hits.append((keyword, re.sub(r"\\s+", " ", js[max(0,pos2-600):pos2+1200])[:1800]))
                        api_paths = list(dict.fromkeys(re.findall(r'[/][A-Za-z0-9._~%+/\\-]*(?:api|research|blog)[A-Za-z0-9._~%+/?=&\\-]*', js, flags=re.I)))[:25]
                        if hits or api_paths:
                            print(f"QWEN JS DEBUG src={src} len={len(js)} api_paths={api_paths}")
                            for kw2, snip2 in hits[:5]:
                                print(f"QWEN JS DEBUG keyword={kw2}: {snip2}")
                for keyword in ("research-list", "latest-advancements", "/api/", "research"):
                    pos = text.lower().find(keyword.lower())
                    if pos >= 0:
                        snippet = re.sub(r"\\s+", " ", text[max(0,pos-350):pos+650])
                        print(f"QWEN DEBUG keyword={keyword}: {snippet[:1000]}")
            for anchor in soup.find_all("a", href=True):
                add_candidate(anchor["href"], anchor.get_text(" ", strip=True))

        normalized = (
            text.replace("\\u0026", "&")
                .replace("\\u003f", "?")
                .replace("\\u003d", "=")
                .replace("\\/", "/")
        )
        patterns = (
            r'https?://(?:www\.)?qwen\.ai/blog\?[^"\'<>\s)]+',
            r'/blog\?[^"\'<>\s)]+',
            r'https?://(?:www\.)?qwen\.ai/research/[A-Za-z0-9._~%+\-/]+',
            r'/research/[A-Za-z0-9._~%+\-/]+',
        )
        for pattern in patterns:
            for match in re.finditer(pattern, normalized, flags=re.I):
                add_candidate(match.group(0))

    # Qwen's research index is client-rendered. Jina's rendered/reader copy often
    # exposes the card links that are absent from the initial HTML response.
    for target in ("https://qwen.ai/research", "https://qwen.ai/home"):
        try:
            r = SESSION.get("https://r.jina.ai/" + target, timeout=25)
            if not r.ok:
                continue
            text = html_lib.unescape(r.text)
        except requests.RequestException:
            continue

        for match in re.finditer(
            r'https?://(?:www\.)?qwen\.ai/(?:blog\?[^)\]>\s]+|research/[A-Za-z0-9._~%+\-/]+)',
            text,
            flags=re.I,
        ):
            add_candidate(match.group(0))

        # Markdown can contain relative targets too.
        for match in re.finditer(
            r'\]\((/blog\?[^)\s]+|/research/[A-Za-z0-9._~%+\-/]+)\)',
            text,
            flags=re.I,
        ):
            add_candidate(match.group(1))

    print(f"Qwen discovery found {len(found)} candidate URLs")
    return [{
        "source": "Qwen Research",
        "title": data.get("title", ""),
        "link": link,
        "description": "",
        "image": "",
        "published": None,
        "guid": make_guid("Qwen Research", link),
    } for link, data in list(found.items())[:100]]


def dedupe(items):
    seen = set()
    out = []
    for item in items:
        key = item.get("link", "").split("#", 1)[0]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def image_type(url):
    path = urllib.parse.urlparse(url).path.lower()
    if path.endswith(".png"):
        return "image/png"
    if path.endswith(".webp"):
        return "image/webp"
    if path.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


def xml_escape(text, quote=False):
    return html_lib.escape(str(text or ""), quote=quote)


def cdata(text):
    return (text or "").replace("]]>", "]]]]><![CDATA[>")


def write_feed(items):
    items = [i for i in items if i.get("title") and i.get("link")]
    items.sort(key=lambda i: i.get("published") or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    items = items[:MAX_OUTPUT]

    now = datetime.now(timezone.utc)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/" xmlns:content="http://purl.org/rss/1.0/modules/content/">',
        '<channel>',
        '<title>Logan’s AI + Tech One Click</title>',
        '<link>https://www.techmeme.com/</link>',
        '<description>Combined direct-link feed: Techmeme, Humanoids Daily, Sherwood News, The AI Timeline by bycloud, and Qwen Research.</description>',
        '<language>en-us</language>',
        f'<lastBuildDate>{xml_escape(rfc822(now))}</lastBuildDate>',
        f'<atom:link href="{xml_escape(SELF, quote=True)}" rel="self" type="application/rss+xml" />',
    ]

    source_urls = {
        "Techmeme": "https://www.techmeme.com/",
        "Humanoids Daily": "https://www.humanoidsdaily.com/",
        "Sherwood News": "https://sherwood.news/",
        "The AI Timeline": "https://mail.bycloud.ai/",
        "Qwen Research": "https://qwen.ai/research",
    }

    for item in items:
        desc = short_desc(item.get("description") or "")
        image = item.get("image") if good_image(item.get("image", "")) else ""
        rich = []
        if image:
            rich.append(f'<p><img src="{xml_escape(image, quote=True)}" alt="" /></p>')
        if desc:
            rich.append(f'<p>{xml_escape(desc)}</p>')

        parts.extend([
            '<item>',
            f'<title>{xml_escape(item["title"])}</title>',
            f'<link>{xml_escape(item["link"])}</link>',
            f'<guid isPermaLink="false">{xml_escape(item.get("guid") or make_guid(item["source"], item["link"]))}</guid>',
            f'<pubDate>{xml_escape(rfc822(item.get("published")))}</pubDate>',
            f'<source url="{xml_escape(source_urls[item["source"]], quote=True)}">{xml_escape(item["source"])}</source>',
            f'<category>{xml_escape(item["source"])}</category>',
            f'<description>{xml_escape(desc)}</description>',
            f'<content:encoded><![CDATA[{cdata("".join(rich))}]]></content:encoded>',
        ])
        if image:
            iu = xml_escape(image, quote=True)
            typ = image_type(image)
            parts.extend([
                f'<media:content url="{iu}" medium="image" type="{typ}" />',
                f'<media:thumbnail url="{iu}" />',
                f'<enclosure url="{iu}" type="{typ}" length="0" />',
            ])
        parts.append('</item>')

    parts.extend(['</channel>', '</rss>', ''])
    OUT.write_text("\n".join(parts), encoding="utf-8")
    print(f"Wrote {len(items)} items to {OUT}")
    counts, images, descriptions = {}, {}, {}
    for i in items:
        s = i["source"]
        counts[s] = counts.get(s, 0) + 1
        images[s] = images.get(s, 0) + bool(i.get("image"))
        descriptions[s] = descriptions.get(s, 0) + bool(i.get("description"))
    print("Counts:", counts)
    print("Images:", images)
    print("Descriptions:", descriptions)


def main():
    cached = load_cached()
    raw = []
    raw.extend(techmeme_items())
    for source, url in SOURCES.items():
        raw.extend(feed_items(url, source))
    raw.extend(discover_bicloud())
    raw.extend(discover_qwen())
    raw = dedupe(raw)

    by_source = {}
    for item in raw:
        by_source.setdefault(item["source"], []).append(item)

    enriched = []
    for source, group in by_source.items():
        limit = MAX_PER_SOURCE[source]
        candidates = group[: (45 if source == "Qwen Research" else limit)]

        def enrich_one(item):
            key = (source, item["link"])
            return enrich(item, cached.get(key), prefer_page_title=source in {"The AI Timeline", "Qwen Research"})

        source_items = []
        workers = min(10, max(1, len(candidates)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(enrich_one, item) for item in candidates]
            for future in as_completed(futures):
                try:
                    item = future.result()
                except Exception as exc:
                    print(f"WARN enrichment failed for {source}: {exc}")
                    continue
                if source == "Qwen Research":
                    if not item.get("published") and not item.get("description"):
                        continue
                    if "/research/" not in item["link"] and "/blog" not in item["link"]:
                        continue
                if not item.get("published"):
                    item["published"] = datetime(1970, 1, 1, tzinfo=timezone.utc)
                source_items.append(item)
        source_items.sort(key=lambda i: i.get("published") or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
        enriched.extend(source_items[:limit])

    write_feed(dedupe(enriched))


if __name__ == "__main__":
    main()
