#!/usr/bin/env python3
"""
check_agency_tiktok.py - Does this agency sell TikTok?

For each agency domain: find the pages where an agency describes what it sells,
read them, and record every mention of TikTok with enough context to audit.
Then run the agency's own domain through pixel_check, because an agency with a
TikTok pixel on its own site is running TikTok somewhere regardless of what the
site copy says.

    python check_agency_tiktok.py acme-marketing.com                  # one agency
    python check_agency_tiktok.py agencies.csv -o checked.csv --sheet
    python check_agency_tiktok.py acme.com --show                     # print the evidence

Evidence is classified, not just counted, because the four ways "tiktok" appears
in an agency's HTML mean four different things:

  service_copy   prose on a services page. The real signal.
  pixel          a TikTok pixel on their own site. The strongest signal, and the
                 one that catches an agency that runs TikTok but doesn't sell it.
  social_link    an href to tiktok.com/@them. That is their own profile, not a
                 service offering, and on its own it is close to meaningless.
  markup_only    a class name or icon reference. Weakest of all - usually a
                 theme's social-icon set shipping every network whether the
                 agency uses it or not.

mentions_tiktok is yes on any hit, per the brief, but the evidence string always
leads with the kind, and notes call out a hit whose only support is a social link
or a stray class name. The target profile - an agency that never mentions TikTok
while it does mention Meta or YouTube - is recorded in notes on every row.
"""

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import common
import pixel_check

# Paths worth trying on every agency, sitemap or not.
CANONICAL_PATHS = [
    "/services", "/what-we-do", "/capabilities", "/solutions", "/paid-media",
    "/social", "/social-media", "/services/paid-media", "/services/social-media",
    "/what-we-do/paid-media", "/expertise", "/our-services", "/advertising",
    "/paid-social", "/media-buying", "/digital-marketing",
]

# A sitemap URL containing any of these is a page where services are described.
SERVICE_URL_WORDS = [
    "service", "what-we-do", "whatwedo", "capabilit", "solution", "paid-media",
    "paidmedia", "social", "advertis", "media-buying", "mediabuying", "ppc",
    "paid-search", "paid-social", "expertise", "channel", "offering", "marketing",
]

MAX_PAGES = 14  # per agency; enough to cover a services tree without hammering

TIKTOK_TERMS = [
    ("tiktok", re.compile(r"tik\s?tok", re.I)),
    ("ttq", re.compile(r"\bttq\b", re.I)),
    ("spark ads", re.compile(r"\bspark\s+ads?\b", re.I)),
]

# The channels that make an agency a target: already producing vertical video.
OTHER_CHANNELS = {
    "meta": re.compile(r"\b(meta ads|facebook ads|instagram ads|meta advertising|"
                       r"facebook advertising|paid social)\b", re.I),
    "youtube": re.compile(r"\b(youtube ads|youtube advertising|youtube|yt shorts)\b", re.I),
    "google": re.compile(r"\b(google ads|google advertising|adwords|paid search|sem)\b", re.I),
    "reels": re.compile(r"\b(reels|short-?form video|vertical video)\b", re.I),
}

_TAG_STRIP_RE = re.compile(r"(?is)<(script|style|noscript|svg)\b.*?</\1>")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def visible_text(html):
    """Body copy only. Scripts and styles are pixel territory, not prose."""
    html = _TAG_STRIP_RE.sub(" ", html or "")
    html = re.sub(r"<br\s*/?>|</(p|div|li|h[1-6]|td)>", " . ", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&#39;", "'").replace("&quot;", '"'))
    return " ".join(text.split())


def hrefs(html):
    return re.findall(r'<a\b[^>]*?href\s*=\s*["\']([^"\'>]+)["\']', html or "", re.I)


def _sentence_around(text, start, end, width=180):
    """The sentence a match sits in, so a hit can be read rather than trusted."""
    left = max(0, start - width)
    right = min(len(text), end + width)
    window = text[left:right]
    offset = start - left
    parts, cursor = _SENTENCE_SPLIT_RE.split(window), 0
    for part in parts:
        if cursor <= offset < cursor + len(part) + 1:
            return " ".join(part.split()).strip(" .")[:240]
        cursor += len(part) + 1
    return " ".join(window.split())[:240]


def find_evidence(page, agency_domain):
    """
    Every TikTok mention on one page, classified. Returns a list of dicts with
    kind / term / url / context.
    """
    out = []
    html = page.text

    # 1. Prose. The signal that matters.
    text = visible_text(html)
    for term, rx in TIKTOK_TERMS:
        for m in rx.finditer(text):
            out.append({
                "kind": "service_copy", "term": term, "url": page.url,
                "context": _sentence_around(text, m.start(), m.end()),
            })
            break  # one example per term per page is enough to audit

    # 2. Links out to TikTok. Almost always their own profile.
    for href in hrefs(html):
        if "tiktok.com" in href.lower():
            handle = href.strip()[:160]
            kind = "social_link"
            if re.search(r"tiktok\.com/(business|ads|partners)", handle, re.I):
                kind = "service_copy"  # linking TikTok for Business is a real signal
            out.append({"kind": kind, "term": "tiktok", "url": page.url,
                        "context": f"link -> {handle}"})
            break

    # 3. Markup-only traces: class names, icon sprites, image filenames. Weak.
    if not out:
        stripped_of_text = re.findall(r'<[^>]+>', html or "")
        for tag in stripped_of_text:
            if re.search(r"tik\s?tok", tag, re.I):
                out.append({"kind": "markup_only", "term": "tiktok", "url": page.url,
                            "context": " ".join(tag.split())[:200]})
                break

    return out


def channels_mentioned(text):
    return sorted(name for name, rx in OTHER_CHANNELS.items() if rx.search(text))


def candidate_pages(fetcher, domain):
    """
    The pages where an agency says what it sells: the sitemap filtered to service
    words, plus the canonical paths, plus service-shaped links off the homepage.
    Returns (urls, note) - note records how the list was arrived at.
    """
    origin = common.base_url(domain)
    if not origin:
        return [], "bad_domain"

    picked, notes = [], []

    sm_urls, sm_note = common.sitemap_urls(fetcher, domain, max_urls=2000)
    notes.append(sm_note)
    if sm_urls:
        for u in sm_urls:
            path = urlparse(u).path.lower()
            if any(w in path for w in SERVICE_URL_WORDS):
                picked.append(u)

    home = fetcher.get(origin)
    if home.ok:
        # The nav is the site's own table of contents; use it when there is no
        # sitemap, and to catch service pages a sitemap omitted.
        nav = common.nav_links(home.text, home.url, domain)
        hits = [u for u in nav
                if any(w in urlparse(u).path.lower() for w in SERVICE_URL_WORDS)]
        notes.append(f"nav:{len(hits)} service links of {len(nav)}")
        picked.extend(hits)
    else:
        notes.append(f"homepage {home.failure}")

    for path in CANONICAL_PATHS:
        picked.append(common.canonical_url(origin + path))

    # Homepage first: many small agencies list every service on it.
    ordered = [common.canonical_url(origin)] + picked
    final = common.dedupe_urls(ordered)
    return final[:MAX_PAGES], "; ".join(notes)


def check_agency(fetcher, rec, run_pixel=True):
    """One agency in, one Agencies-shaped row out. Never raises for a bad site."""
    domain = common.root_domain(rec.get("agency_domain") or rec.get("domain") or "")
    row = dict(rec)
    row["agency_domain"] = domain or (rec.get("agency_domain") or "")
    row.setdefault("agency_name", "")
    row.setdefault("vertical", "")

    if not domain:
        row["status"] = "bad_input"
        row["mentions_tiktok"] = ""
        row["notes"] = "not a parseable domain"
        return row

    urls, page_note = candidate_pages(fetcher, domain)
    evidence, fetched, failed, all_text = [], [], [], []

    for url in urls:
        page = fetcher.get(url)
        if not page.ok:
            failed.append(f"{urlparse(url).path or '/'}={page.failure}")
            continue
        fetched.append(url)
        evidence.extend(find_evidence(page, domain))
        all_text.append(visible_text(page.text))

    joined = " ".join(all_text)
    channels = channels_mentioned(joined)

    # The agency's own pixel. Reuses pixel_check rather than re-implementing it.
    pixel_row = {}
    if run_pixel:
        session = pixel_check.requests.Session()
        try:
            pixel_row = pixel_check.check(domain, session)
        except Exception as e:
            pixel_row = {"status": f"crashed:{type(e).__name__}"}
        finally:
            session.close()
        if pixel_row.get("tiktok") == "yes":
            evidence.insert(0, {
                "kind": "pixel", "term": "tiktok pixel",
                "url": pixel_row.get("resolved_url", domain),
                "context": pixel_row.get("tiktok_evidence", "TikTok pixel detected"),
            })

    # Strongest evidence first, so the first line of the cell is the one to read.
    rank = {"pixel": 0, "service_copy": 1, "social_link": 2, "markup_only": 3}
    evidence.sort(key=lambda e: rank.get(e["kind"], 9))
    kinds = {e["kind"] for e in evidence}

    row["mentions_tiktok"] = "yes" if evidence else "no"
    row["tiktok_evidence"] = " || ".join(
        f"[{e['kind']}] {e['url']} :: {e['context']}" for e in evidence[:6])

    notes = [f"pages_fetched={len(fetched)}/{len(urls)}", f"pages={page_note}"]
    if channels:
        notes.append("mentions=" + ",".join(channels))
    else:
        notes.append("mentions=none_detected")
    if evidence and kinds <= {"social_link", "markup_only"}:
        notes.append("WEAK: only a social-profile link or a stray class name")
    if not evidence and any(c in channels for c in ("meta", "youtube", "reels")):
        notes.append("TARGET: runs Meta/YouTube/vertical video, no TikTok mention")
    if pixel_row:
        own = [p for p in ("tiktok", "meta", "google_ads")
               if pixel_row.get(p) == "yes"]
        notes.append("own_site_pixels=" + (",".join(own) if own else "none"))
        if pixel_row.get("status") not in ("ok", ""):
            notes.append(f"own_site_pixel_check={pixel_row.get('status')}")
    if failed:
        notes.append("failed=" + ",".join(failed[:6]))

    if not fetched:
        # Zero fetched pages is never "this agency does not mention TikTok".
        row["status"] = "unreachable"
        row["mentions_tiktok"] = ""
        row["tiktok_evidence"] = ""
        notes.insert(0, "NO PAGES FETCHED - mentions_tiktok left blank, not 'no'")
    elif len(fetched) < 2 and not evidence:
        row["status"] = "needs_manual_review"
        notes.insert(0, "only one page read; a single-page read is not a clean no")
    else:
        row["status"] = "checked"

    prior = (rec.get("notes") or "").strip()
    row["notes"] = " | ".join(([prior] if prior else []) + notes)
    row["last_checked"] = common.now_stamp()
    row["_pages_fetched"] = len(fetched)
    row["_pixel_status"] = pixel_row.get("status", "")
    row["_own_tiktok_pixel"] = pixel_row.get("tiktok", "")
    return row


OUT_FIELDS = ["agency_name", "agency_domain", "vertical", "hq_location",
              "employee_count", "mentions_tiktok", "tiktok_evidence",
              "client_page_url", "clients_found", "status", "notes", "last_checked",
              "_pages_fetched", "_pixel_status", "_own_tiktok_pixel"]


def main():
    ap = argparse.ArgumentParser(
        description="Check whether each agency sells TikTok, with auditable evidence.")
    ap.add_argument("input",
                    help="an agency domain, a comma-separated list, or a CSV "
                         "with an agency_domain column")
    ap.add_argument("-c", "--column", default="agency_domain",
                    help="column holding the domain (default agency_domain)")
    ap.add_argument("-o", "--output", default="agency_tiktok.csv")
    ap.add_argument("--no-pixel", action="store_true",
                    help="skip running the agency's own domain through pixel_check")
    ap.add_argument("--show", action="store_true",
                    help="print the full evidence for each agency as it completes")
    ap.add_argument("--limit", type=int, default=0, help="stop after N agencies")
    common.add_crawl_args(ap)
    common.add_sheet_args(ap)
    args = ap.parse_args()

    # Keep whatever the input file already knew about each agency - name,
    # vertical, hq - so this step enriches rows instead of blanking them.
    import csv
    import os
    seeds = []
    if os.path.exists(args.input) and args.input.lower().endswith(".csv"):
        with open(args.input, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            col = args.column if args.column in (reader.fieldnames or []) else None
            if not col:
                for alt in ("agency_domain", "domain", "website", "url"):
                    if alt in (reader.fieldnames or []):
                        col = alt
                        print(f"Column '{args.column}' not found; using '{col}'.",
                              file=sys.stderr)
                        break
            if not col:
                sys.exit(f"No domain column in {args.input}. "
                         f"Available: {reader.fieldnames}")
            for r in reader:
                if (r.get(col) or "").strip():
                    seeds.append(dict(r, agency_domain=r[col].strip()))
    else:
        seeds = [{"agency_domain": d}
                 for d in common.read_domain_list(args.input, args.column)]

    # Dedupe by root domain before spending any requests.
    seen, agencies = set(), []
    for s in seeds:
        rd = common.root_domain(s["agency_domain"])
        key = rd or s["agency_domain"]
        if key not in seen:
            seen.add(key)
            agencies.append(s)
    if args.limit:
        agencies = agencies[:args.limit]
    if not agencies:
        sys.exit(f"No agency domains found in {args.input!r}.")

    print(f"Checking {len(agencies)} agencies with {args.workers} workers "
          f"({args.delay}s/request per host)...", file=sys.stderr)

    fetcher = common.fetcher_from_args(args)
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(check_agency, fetcher, a, not args.no_pixel): a
                   for a in agencies}
        for i, fut in enumerate(as_completed(futures), 1):
            src = futures[fut]
            try:
                row = fut.result()
            except Exception as e:
                row = dict(src, status=f"crashed:{type(e).__name__}",
                           mentions_tiktok="", notes=f"crashed: {e}")
            results.append(row)
            flag = {"yes": "TIKTOK", "no": "clean ", "": "??????"}.get(
                row.get("mentions_tiktok", ""), "??????")
            print(f"  [{i}/{len(agencies)}] {flag} {row.get('agency_domain', '')} "
                  f"({row.get('status', '')})", file=sys.stderr)
            if args.show and row.get("tiktok_evidence"):
                for line in row["tiktok_evidence"].split(" || "):
                    print(f"        {line}", file=sys.stderr)

    order = {common.root_domain(a["agency_domain"]) or a["agency_domain"]: i
             for i, a in enumerate(agencies)}
    results.sort(key=lambda r: order.get(r.get("agency_domain", ""), 1 << 30))

    common.report_stats(fetcher)
    yes = sum(1 for r in results if r.get("mentions_tiktok") == "yes")
    no = sum(1 for r in results if r.get("mentions_tiktok") == "no")
    blank = len(results) - yes - no
    targets = sum(1 for r in results if "TARGET:" in (r.get("notes") or ""))
    print(f"\n  mentions TikTok: {yes}", file=sys.stderr)
    print(f"  no mention:      {no}", file=sys.stderr)
    print(f"  undetermined:    {blank}  (unreachable or blocked - not a clean no)",
          file=sys.stderr)
    print(f"  target profile:  {targets}  (no TikTok, but Meta/YouTube/vertical video)",
          file=sys.stderr)

    if args.dry_run:
        print("\n--dry-run: nothing written.", file=sys.stderr)
        return

    common.write_csv(args.output, OUT_FIELDS, results)
    if args.sheet:
        common.push_sheet("Agencies", results, args.sheet_id, args.output)


if __name__ == "__main__":
    main()
