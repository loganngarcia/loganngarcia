#!/usr/bin/env python3
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime
from html import escape
from pathlib import Path

API = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?actor=techmeme.com&limit=100&filter=posts_no_replies"
OUT = Path(__file__).with_name("feed.xml")
SELF = "https://raw.githubusercontent.com/loganngarcia/loganngarcia/main/techmeme-oneclick/feed.xml"

UA = "TechmemeOneClick/1.0 (+https://github.com/loganngarcia/loganngarcia)"

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

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
        if url in seen:
            continue
        seen.add(url)
        try:
            host = (urllib.parse.urlparse(url).hostname or "").lower()
        except Exception:
            continue
        if host.endswith("techmeme.com") or host.endswith("bsky.app"):
            continue
        clean.append(url)
    return clean

def clean_title(text):
    text = re.sub(r"\s*Main Link\s*\|\s*Techmeme Permalink\s*$", "", text or "", flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def rfc822(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return format_datetime(dt.astimezone(timezone.utc))
    except Exception:
        return format_datetime(datetime.now(timezone.utc))

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
        created = record.get("createdAt") or post.get("indexedAt")
        guid = post.get("uri") or link
        source = urllib.parse.urlparse(link).hostname or ""
        items.append({
            "title": title,
            "link": link,
            "guid": guid,
            "pubDate": rfc822(created or ""),
            "source": source,
        })

    now = format_datetime(datetime.now(timezone.utc))
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        '<channel>',
        '<title>Techmeme One Click</title>',
        '<link>https://www.techmeme.com/river</link>',
        '<description>Techmeme headlines that open the original publisher directly.</description>',
        '<language>en-us</language>',
        f'<lastBuildDate>{escape(now)}</lastBuildDate>',
        f'<atom:link href="{escape(SELF, quote=True)}" rel="self" type="application/rss+xml" />',
    ]
    for item in items:
        parts.extend([
            '<item>',
            f'<title>{escape(item["title"])}</title>',
            f'<link>{escape(item["link"])}</link>',
            f'<guid isPermaLink="false">{escape(item["guid"])}</guid>',
            f'<pubDate>{escape(item["pubDate"])}</pubDate>',
            f'<description>{escape("Source: " + item["source"])}</description>',
            '</item>',
        ])
    parts.extend(['</channel>', '</rss>', ''])
    OUT.write_text("\n".join(parts), encoding="utf-8")
    print(f"Wrote {len(items)} direct-link items to {OUT}")

if __name__ == "__main__":
    build()
