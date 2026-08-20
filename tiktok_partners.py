#!/usr/bin/env python3
"""
tiktok_partners.py - The suppression list.

Scrapes the TikTok Marketing Partner directory at partners.tiktok.com/directory
once, caches it to data/tiktok_partners.json, and exposes a membership test that
find_agencies.py uses to drop badged partners from the candidate pool.

Read this before trusting it: the directory lists on the order of 500 companies
globally, and almost no small agency is badged. As a filter it removes the few
obvious wrong answers and nothing else. It is suppression, not signal. An agency
absent from this list has told you nothing.

The directory is a JavaScript application, so there are three ways in and we try
all of them:

  1. the JSON API the page itself calls
  2. state embedded in the HTML (__NEXT_DATA__ / __INITIAL_STATE__ / inline JSON)
  3. plain anchor text in the served HTML

If every route comes back empty we say so and refuse to write the cache. An empty
suppression list that looks successful is worse than no list at all: every run
after it would report "not a TikTok partner" for companies that are.

Usage:
    python tiktok_partners.py --refresh          # scrape and cache
    python tiktok_partners.py                    # show what's cached
    python tiktok_partners.py --from-file p.json # load a capture you saved
    python tiktok_partners.py --check acme.com   # membership test
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

import common

CACHE_PATH = os.path.join("data", "tiktok_partners.json")
DIRECTORY_URL = "https://partners.tiktok.com/directory"

# Endpoints the directory app is known to call, in the order worth trying. These
# are not a documented API and TikTok changes them without notice - when a
# refresh comes back empty, open the directory in a browser, watch the Network
# tab, and add whatever it actually calls to this list.
API_CANDIDATES = [
    "https://partners.tiktok.com/api/v1/directory/partner/list?page=1&page_size=1000",
    "https://partners.tiktok.com/api/directory/partners?page=1&limit=1000",
    "https://partners.tiktok.com/api/v1/partner/search?page=1&page_size=1000",
]

# Keys that plausibly hold a partner's website or name, at any depth.
_URL_KEYS = {"website", "web_site", "site", "url", "company_url", "homepage",
             "company_website", "partner_url", "link"}
_NAME_KEYS = {"name", "company_name", "partner_name", "title", "display_name",
              "companyName", "partnerName", "displayName"}

_JSON_BLOB_RE = re.compile(
    r'<script[^>]*(?:id=["\'](?:__NEXT_DATA__|__INITIAL_STATE__)["\']|'
    r'type=["\']application/json["\'])[^>]*>(.*?)</script>',
    re.I | re.S)
_STATE_ASSIGN_RE = re.compile(
    r'window\.(?:__INITIAL_STATE__|__NUXT__|__DATA__)\s*=\s*(\{.*?\})\s*[;<]', re.S)

# Domains that appear on the directory page but are not partners.
_NOISE = {
    "tiktok.com", "bytedance.com", "byteoversea.com", "tiktokglobalshop.com",
    "facebook.com", "twitter.com", "linkedin.com", "instagram.com", "youtube.com",
    "google.com", "apple.com", "microsoft.com",
}


def _walk(node, out):
    """Pull (name, domain) pairs out of an arbitrarily shaped JSON tree."""
    if isinstance(node, dict):
        name = next((str(node[k]).strip() for k in _NAME_KEYS
                     if isinstance(node.get(k), str) and node[k].strip()), "")
        domain = ""
        for k in _URL_KEYS:
            v = node.get(k)
            if isinstance(v, str) and v.strip():
                rd = common.root_domain(v)
                if rd and rd not in _NOISE:
                    domain = rd
                    break
        if domain or (name and len(name) < 80 and any(k in node for k in _URL_KEYS)):
            out.append({"name": name, "domain": domain})
        for v in node.values():
            _walk(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out)
    return out


def _dedupe(records):
    by_domain, nameless = {}, {}
    for r in records:
        d, n = (r.get("domain") or "").strip(), (r.get("name") or "").strip()
        if d:
            if d not in by_domain or (n and not by_domain[d].get("name")):
                by_domain[d] = {"name": n, "domain": d}
        elif n:
            nameless.setdefault(n.lower(), {"name": n, "domain": ""})
    out = sorted(by_domain.values(), key=lambda r: r["domain"])
    out += sorted(nameless.values(), key=lambda r: r["name"].lower())
    return out


def scrape(fetcher):
    """
    Try every route into the directory. Returns (records, trace) where trace is
    a list of "route -> what happened" strings. The trace is printed on every
    refresh so a zero result is always explained rather than assumed.
    """
    records, trace = [], []

    for api in API_CANDIDATES:
        page = fetcher.get(api, allow_robots_override=False)
        if not page.ok:
            trace.append(f"api {api} -> {page.failure}")
            continue
        try:
            data = json.loads(page.text)
        except ValueError:
            trace.append(f"api {api} -> 200 but not JSON")
            continue
        found = _walk(data, [])
        trace.append(f"api {api} -> {len(found)} candidate records")
        records.extend(found)

    page = fetcher.get(DIRECTORY_URL)
    if not page.ok:
        trace.append(f"html {DIRECTORY_URL} -> {page.failure}")
    else:
        html = page.text
        blobs = _JSON_BLOB_RE.findall(html) + _STATE_ASSIGN_RE.findall(html)
        embedded = 0
        for blob in blobs:
            try:
                data = json.loads(blob)
            except ValueError:
                continue
            found = _walk(data, [])
            embedded += len(found)
            records.extend(found)
        trace.append(f"html embedded JSON -> {len(blobs)} blob(s), "
                     f"{embedded} candidate records")

        anchors = []
        for href, text in re.findall(
                r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
            rd = common.root_domain(href)
            if rd and rd not in _NOISE:
                label = re.sub(r"<[^>]+>", " ", text)
                anchors.append({"name": " ".join(label.split())[:80], "domain": rd})
        trace.append(f"html outbound anchors -> {len(anchors)} candidate records")
        records.extend(anchors)

    return _dedupe(records), trace


def load(path=CACHE_PATH):
    """Read the cached list. Returns (records, meta). Missing cache -> ([], {})."""
    if not os.path.exists(path):
        return [], {}
    try:
        with open(path, encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit(f"Partner cache at {path} is unreadable: {e}\n"
                         f"Delete it and re-run with --refresh.")
    return blob.get("partners", []), blob.get("meta", {})


def save(records, trace, path=CACHE_PATH):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "meta": {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "count": len(records),
                "with_domain": sum(1 for r in records if r.get("domain")),
                "trace": trace,
            },
            "partners": records,
        }, f, indent=2)
    print(f"Cached {len(records)} partner records -> {path}", file=sys.stderr)


def domain_set(path=CACHE_PATH, required=True):
    """
    The suppression set: root domains of badged partners.

    required=True makes a missing or empty cache a hard stop. Suppression that
    quietly matches nothing is the failure mode this whole module exists to
    avoid, so the default is to refuse to run rather than to run toothless.
    """
    records, meta = load(path)
    domains = {r["domain"] for r in records if r.get("domain")}
    if not domains and required:
        raise SystemExit(
            f"The TikTok partner suppression list at {path} is empty or missing.\n\n"
            "Populate it first:\n"
            "    python tiktok_partners.py --refresh\n\n"
            "If the refresh cannot reach partners.tiktok.com, open\n"
            f"    {DIRECTORY_URL}\n"
            "in a browser, save the page or the directory JSON response, and load it:\n"
            "    python tiktok_partners.py --from-file saved.json\n\n"
            "Or run find_agencies.py with --no-suppression to proceed without it,\n"
            "which is a real choice with a real cost, not a default.\n"
        )
    if meta.get("fetched_at"):
        print(f"Suppression list: {len(domains)} partner domains "
              f"(cached {meta['fetched_at'][:10]})", file=sys.stderr)
    return domains


def name_set(path=CACHE_PATH):
    records, _ = load(path)
    return {r["name"].strip().lower() for r in records if r.get("name")}


def is_partner(domain, partner_domains):
    rd = common.root_domain(domain)
    return bool(rd) and rd in partner_domains


def main():
    ap = argparse.ArgumentParser(description="Build and inspect the TikTok partner suppression list.")
    ap.add_argument("--refresh", action="store_true", help="re-scrape the directory")
    ap.add_argument("--from-file", help="parse a saved directory HTML or JSON capture instead")
    ap.add_argument("--check", help="test whether a domain is a badged partner")
    ap.add_argument("--path", default=CACHE_PATH, help=f"cache location (default {CACHE_PATH})")
    common.add_crawl_args(ap)
    args = ap.parse_args()

    if args.check:
        partners = domain_set(args.path, required=False)
        if not partners:
            print("No suppression list cached - run --refresh first.", file=sys.stderr)
        rd = common.root_domain(args.check)
        print(f"{args.check} -> root={rd} partner={'YES' if rd in partners else 'no'}")
        return

    if args.from_file:
        if not os.path.exists(args.from_file):
            sys.exit(f"No such file: {args.from_file}")
        with open(args.from_file, encoding="utf-8", errors="replace") as f:
            raw = f.read()
        records, trace = [], []
        try:
            records = _walk(json.loads(raw), [])
            trace.append(f"file {args.from_file} -> parsed as JSON, {len(records)} records")
        except ValueError:
            for blob in _JSON_BLOB_RE.findall(raw) + _STATE_ASSIGN_RE.findall(raw):
                try:
                    records.extend(_walk(json.loads(blob), []))
                except ValueError:
                    continue
            for href, text in re.findall(
                    r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', raw, re.I | re.S):
                rd = common.root_domain(href)
                if rd and rd not in _NOISE:
                    label = re.sub(r"<[^>]+>", " ", text)
                    records.append({"name": " ".join(label.split())[:80], "domain": rd})
            trace.append(f"file {args.from_file} -> parsed as HTML, {len(records)} records")
        records = _dedupe(records)
        if not records:
            sys.exit(f"Parsed {args.from_file} and found no partner records. "
                     f"Not writing an empty cache.")
        save(records, trace, args.path)
        return

    if args.refresh:
        fetcher = common.fetcher_from_args(args)
        records, trace = scrape(fetcher)
        print("\nRefresh trace:", file=sys.stderr)
        for line in trace:
            print(f"  {line}", file=sys.stderr)
        common.report_stats(fetcher)
        if not records:
            sys.exit(
                "\nEvery route into the directory came back empty, so nothing was "
                "cached.\nThe existing cache, if any, is untouched.\n\n"
                "The directory is a JavaScript app and TikTok changes its endpoints. "
                "Open it in a\nbrowser, save either the page HTML or the JSON the "
                "directory request returns, then:\n"
                f"    python tiktok_partners.py --from-file <that file>\n"
            )
        save(records, trace, args.path)
        return

    records, meta = load(args.path)
    if not records:
        print(f"No cache at {args.path}. Run: python tiktok_partners.py --refresh")
        return
    with_domain = sum(1 for r in records if r.get("domain"))
    print(f"{len(records)} partner records cached at {args.path}")
    print(f"  fetched: {meta.get('fetched_at', 'unknown')}")
    print(f"  with a resolvable domain: {with_domain}")
    for r in records[:25]:
        print(f"  {r.get('domain', ''):40} {r.get('name', '')}")
    if len(records) > 25:
        print(f"  ... and {len(records) - 25} more")


if __name__ == "__main__":
    main()
