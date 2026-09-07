#!/usr/bin/env python3
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path

API = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?actor=techmeme.com&limit=100&filter=posts_no_replies"
OUT = Path(__file__).with_name("feed.xml")
SELF = "https://raw.githubusercontent.com/loganngarcia/loganngarcia/main/techmeme-oneclick/feed.xml"

UA = "Mozilla/5.0 (compatible; TechmemeOneClick/2.0; +https://github.com/loganngarcia/loganngarcia)"

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def is_techmeme(url):
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return True
    return host.endswith("techmeme.com") or host.endswith("bsky.app")

def direct_links(post):
    out = []
    record = post.get("record") or {}
    for facet in record.get("facets") or []:
        for feature in facet.get("features") or []:
            uri = feature.get("uri")
            if uri:
                out.append(uri)

    embed = post.get("embed") or {}
    ext = embed.get("external") or {}
    if ext.get("uri"):
        out.append(ext["uri"])

    seen = set()
    clean = []
    for url in out:
        if not url or url in seen or is_techmeme(url):
            continue
        seen.add(url)
        clean.append(url)
    return clean

def clean_title(text):
    text = re.sub(r"\s*Main Link\s*\|\s*Techmeme Permalink\s*$", "", text or "", flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def clean_text(text):
    return re.sub(r"\s+", " ", text or "").strip()

def rfc822(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return format_datetime(dt.astimezone(timezone.utc))
    except Exception:
        return format_datetime(datetime.now(timezone.utc))

def cdata(text):
    return (text or "").replace("]]>", "]]]]><![CDATA[>")

class MetaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return
        d = {str(k).lower(): v for k, v in attrs if k}
        key = (d.get("property") or d.get("name") or "").lower()
        value = d.get("content")
        if key and value and key not in self.meta:
            self.meta[key] = value

def fetch_open_graph(url):
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "html" not in ctype:
                return "", ""
            raw = r.read(512 * 1024)
            final_url = r.geturl()
        html = raw.decode("utf-8", errors="ignore")
        parser = MetaParser()
        parser.feed(html)
        m = parser.meta
        description = clean_text(
            m.get("og:description")
            or m.get("twitter:description")
            or m.get("description")
            or ""
        )
        image = (
            m.get("og:image:secure_url")
            or m.get("og:image")
            or m.get("twitter:image")
            or m.get("twitter:image:src")
            or ""
        )
        if image:
            image = urllib.parse.urljoin(final_url, image)
        return description, image
    except Exception:
        return "", ""

def embed_preview(post, link):
    embed = post.get("embed") or {}
    ext = embed.get("external") or {}
    uri = ext.get("uri") or ""
    if not uri or is_techmeme(uri):
        return "", ""

    try:
        link_host = (urllib.parse.urlparse(link).hostname or "").lower()
        uri_host = (urllib.parse.urlparse(uri).hostname or "").lower()
    except Exception:
        return "", ""

    if link_host != uri_host:
        return "", ""

    return clean_text(ext.get("description") or ""), ext.get("thumb") or ""

def build():
    data = fetch_json(API)
    items = []

    for entry in data.get("feed") or []:
        post = entry.get("post") or {}
        record = post.get("record") or {}
        title = clean_title(record.get("text", ""))
        links = direct_links(post)
        if not title or not links:
            continue

        link = links[0]
        description, image = embed_preview(post, link)

        # Bluesky's external card normally already contains the article's
        # Open Graph description and thumbnail. Only hit the publisher when
        # either field is missing.
        if not description or not image:
            og_description, og_image = fetch_open_graph(link)
            description = description or og_description
            image = image or og_image

        created = record.get("createdAt") or post.get("indexedAt")
        guid = post.get("uri") or link
        source = urllib.parse.urlparse(link).hostname or ""

        items.append({
            "title": title,
            "link": link,
            "guid": guid,
            "pubDate": rfc822(created or ""),
            "source": source,
            "description": description,
            "image": image,
        })

    now = format_datetime(datetime.now(timezone.utc))
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/" xmlns:content="http://purl.org/rss/1.0/modules/content/">',
        '<channel>',
        '<title>Techmeme One Click</title>',
        '<link>https://www.techmeme.com/river</link>',
        '<description>Techmeme headlines that open the original publisher directly, with article previews.</description>',
        '<language>en-us</language>',
        f'<lastBuildDate>{escape(now)}</lastBuildDate>',
        f'<atom:link href="{escape(SELF, quote=True)}" rel="self" type="application/rss+xml" />',
    ]

    for item in items:
        description = item["description"] or ("From " + item["source"])
        image = item["image"]

        html_parts = []
        if image:
            html_parts.append(
                '<p><img src="' + escape(image, quote=True) + '" alt="" /></p>'
            )
        if description:
            html_parts.append('<p>' + escape(description) + '</p>')
        rich_html = "".join(html_parts)

        parts.extend([
            '<item>',
            f'<title>{escape(item["title"])}</title>',
            f'<link>{escape(item["link"])}</link>',
            f'<guid isPermaLink="false">{escape(item["guid"])}</guid>',
            f'<pubDate>{escape(item["pubDate"])}</pubDate>',
            f'<description>{escape(description)}</description>',
            f'<content:encoded><![CDATA[{cdata(rich_html)}]]></content:encoded>',
        ])

        if image:
            image_xml = escape(image, quote=True)
            parts.extend([
                f'<media:content url="{image_xml}" medium="image" />',
                f'<media:thumbnail url="{image_xml}" />',
            ])

        parts.append('</item>')

    parts.extend(['</channel>', '</rss>', ''])
    OUT.write_text("\n".join(parts), encoding="utf-8")
    print(f"Wrote {len(items)} direct-link items with previews to {OUT}")

if __name__ == "__main__":
    build()
