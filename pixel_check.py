#!/usr/bin/env python3
"""
pixel_check.py - Detect advertising pixels on a list of websites.

Identifies which ad platforms a brand is set up to run conversion campaigns on,
by inspecting the live site rather than relying on a technographics vendor.

Two detection layers:
  1. Static HTML  - regex the homepage source for pixel loader signatures.
  2. GTM container - pull the GTM-XXXXXXX id out of the page, fetch the public
                     container at googletagmanager.com/gtm.js, and scan its tag
                     manifest. This is the important one: tags that only fire on
                     cart/checkout/purchase pages are invisible to a homepage-only
                     scan, and TikTok conversion pixels usually live exactly there.

Layer 3 (headless browser) is intentionally not included - see NOTES at bottom.

Usage:
    python pixel_check.py input.csv -o results.csv
    python pixel_check.py input.csv -o results.csv --workers 20 --column domain

Input CSV needs a column of domains or URLs (default column name: "domain").
Bare domains are fine - "acme.com", "www.acme.com", and "https://acme.com" all work.
"""

import argparse
import csv
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

requests.packages.urllib3.disable_warnings()

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

TIMEOUT = 20
GTM_URL = "https://www.googletagmanager.com/gtm.js?id={}"

# Signature sets. Each platform maps to a list of (label, compiled regex).
# Labels are recorded as evidence so a hit can be audited rather than trusted.
SIGNATURES = {
    "tiktok": [
        ("events.js loader", re.compile(r"analytics\.tiktok\.com/i18n/pixel/events\.js", re.I)),
        ("ttq.load", re.compile(r"\bttq\s*\.\s*load\s*\(", re.I)),
        ("ttq.page/track", re.compile(r"\bttq\s*\.\s*(page|track|identify)\s*\(", re.I)),
        ("pixel endpoint", re.compile(r"analytics\.tiktok\.com/api/v\d+/pixel", re.I)),
        ("gtm template", re.compile(r"tiktok[_-]?(pixel|ads|conversion)", re.I)),
    ],
    "meta": [
        ("fbevents.js loader", re.compile(r"connect\.facebook\.net/[^/\"']+/fbevents\.js", re.I)),
        ("fbq init", re.compile(r"\bfbq\s*\(\s*['\"]init['\"]", re.I)),
        ("tr pixel", re.compile(r"facebook\.com/tr\?id=", re.I)),
        ("pixel id", re.compile(r"\bfbq\s*\(\s*['\"]init['\"]\s*,\s*['\"](\d{15,16})['\"]", re.I)),
    ],
    "google_ads": [
        ("gtag AW loader", re.compile(r"googletagmanager\.com/gtag/js\?id=AW-", re.I)),
        ("conversion id", re.compile(r"\bAW-\d{9,11}\b")),
        ("googleadservices", re.compile(r"googleadservices\.com/pagead/conversion", re.I)),
        ("doubleclick remarketing", re.compile(r"googleads\.g\.doubleclick\.net", re.I)),
        ("legacy conversion", re.compile(r"google_conversion_id", re.I)),
    ],
    "floodlight": [
        # Floodlight implies DV360 / CM360 - a stronger signal of programmatic
        # video buying than a plain gtag, and worth separating out.
        ("fls.doubleclick", re.compile(r"[\w.-]*\bfls\.doubleclick\.net", re.I)),
        ("dc_ tags", re.compile(r"\bdc_(iu|rdid|pre)\b", re.I)),
    ],
    "pinterest": [
        ("pintrk", re.compile(r"\bpintrk\s*\(", re.I)),
        ("pinterest tag", re.compile(r"s\.pinimg\.com/ct/core\.js", re.I)),
    ],
    "snapchat": [
        ("snaptr", re.compile(r"\bsnaptr\s*\(", re.I)),
        ("sc-static", re.compile(r"sc-static\.net/scevent\.min\.js", re.I)),
    ],
}

GTM_ID_RE = re.compile(r"\bGTM-[A-Z0-9]{4,9}\b")
GA4_RE = re.compile(r"\bG-[A-Z0-9]{8,12}\b")


def normalize(raw):
    """Turn whatever the CSV gave us into a fetchable https URL."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = parsed.netloc
    # A real hostname has a dot, no whitespace, and a plausible TLD. Without this
    # check a stray brand name from the CSV becomes "https://Acme Plumbing" and
    # burns a timeout instead of failing fast.
    if not host or " " in host or "." not in host:
        return None
    if not re.match(r"^[A-Za-z0-9.\-:]+$", host) or not re.search(r"\.[A-Za-z]{2,}$", host.split(":")[0]):
        return None
    return f"{parsed.scheme}://{host}"


def fetch(url, session):
    """GET a URL, tolerating the usual small-site TLS and redirect messes."""
    try:
        r = session.get(url, timeout=TIMEOUT, headers={"User-Agent": UA},
                        allow_redirects=True, verify=True)
        return r.status_code, r.text
    except requests.exceptions.SSLError:
        # Plenty of contractor and med-spa sites have broken certs. Retry
        # unverified rather than losing the row - we only read markup.
        try:
            r = session.get(url, timeout=TIMEOUT, headers={"User-Agent": UA},
                            allow_redirects=True, verify=False)
            return r.status_code, r.text
        except Exception as e:
            return None, f"__ERROR__{type(e).__name__}"
    except Exception as e:
        return None, f"__ERROR__{type(e).__name__}"


def scan(text):
    """Run every signature against a blob of markup or JS."""
    found = {}
    for platform, sigs in SIGNATURES.items():
        hits = [label for label, rx in sigs if rx.search(text)]
        if hits:
            found[platform] = hits
    return found


def merge(base, extra, source_label):
    """Fold a second scan's hits into the first, tagging where they came from."""
    for platform, hits in extra.items():
        tagged = [f"{h} ({source_label})" for h in hits]
        base.setdefault(platform, []).extend(tagged)
    return base


def check(domain, session, include_gtm=True):
    row = {
        "domain": domain, "resolved_url": "", "http_status": "", "status": "",
        "gtm_ids": "", "ga4_ids": "", "qualifies": "no",
    }
    for p in SIGNATURES:
        row[p] = ""
        row[f"{p}_evidence"] = ""

    url = normalize(domain)
    if not url:
        row["status"] = "bad_input"
        return row
    row["resolved_url"] = url

    code, body = fetch(url, session)
    if code is None:
        row["status"] = body.replace("__ERROR__", "fetch_failed:")
        return row
    row["http_status"] = code
    if code >= 400 or not body:
        row["status"] = "unreachable"
        return row

    found = scan(body)

    gtm_ids = sorted(set(GTM_ID_RE.findall(body)))
    row["gtm_ids"] = "|".join(gtm_ids)
    row["ga4_ids"] = "|".join(sorted(set(GA4_RE.findall(body))))

    # The container scan. Worth the extra request on every row that has one.
    if include_gtm:
        for gid in gtm_ids[:3]:  # cap: a page with 10 containers is a tag-manager mess
            gcode, gbody = fetch(GTM_URL.format(gid), session)
            if gcode == 200 and gbody and not gbody.startswith("__ERROR__"):
                found = merge(found, scan(gbody), f"GTM:{gid}")

    for platform in SIGNATURES:
        if platform in found:
            row[platform] = "yes"
            row[f"{platform}_evidence"] = "; ".join(dict.fromkeys(found[platform]))
        else:
            row[platform] = "no"

    row["status"] = "ok"
    # The actual qualification: running Meta or Google, and no TikTok anywhere.
    row["qualifies"] = "yes" if (
        row["status"] == "ok"
        and row["tiktok"] == "no"
        and (row["meta"] == "yes" or row["google_ads"] == "yes")
    ) else "no"
    return row


def main():
    ap = argparse.ArgumentParser(description="Detect ad pixels across a list of sites.")
    ap.add_argument("input", help="CSV containing a column of domains")
    ap.add_argument("-o", "--output", default="pixel_results.csv")
    ap.add_argument("-c", "--column", default="domain", help="column name holding domains")
    ap.add_argument("-w", "--workers", type=int, default=10)
    ap.add_argument("--no-gtm", action="store_true", help="skip GTM container parsing (faster, misses a lot)")
    ap.add_argument("--delay", type=float, default=0.0, help="seconds to sleep per request, per worker")
    ap.add_argument("--sheet", action="store_true",
                    help="also write results to the 'Ad Tags' tab in Google Sheets")
    ap.add_argument("--sheet-id", default=None, help="override the target workbook id")
    ap.add_argument("--agency", default="", help="tag these rows with an agency name")
    args = ap.parse_args()

    with open(args.input, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if args.column not in reader.fieldnames:
            sys.exit(f"Column '{args.column}' not found. Available: {reader.fieldnames}")
        domains = [r[args.column] for r in reader if r.get(args.column, "").strip()]

    print(f"Checking {len(domains)} domains with {args.workers} workers...", file=sys.stderr)

    results = []
    session_factory = lambda: requests.Session()

    def worker(d):
        s = session_factory()
        try:
            if args.delay:
                time.sleep(args.delay)
            return check(d, s, include_gtm=not args.no_gtm)
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(worker, d): d for d in domains}
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({"domain": futures[fut], "status": f"crashed:{type(e).__name__}"})
            if i % 25 == 0:
                print(f"  {i}/{len(domains)}", file=sys.stderr)

    order = ["domain", "resolved_url", "http_status", "status", "qualifies",
             "tiktok", "meta", "google_ads", "floodlight", "pinterest", "snapchat",
             "gtm_ids", "ga4_ids"] + [f"{p}_evidence" for p in SIGNATURES]

    by_domain = {d: i for i, d in enumerate(domains)}
    results.sort(key=lambda r: by_domain.get(r.get("domain", ""), 1 << 30))

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=order, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)

    ok = sum(1 for r in results if r.get("status") == "ok")
    qual = sum(1 for r in results if r.get("qualifies") == "yes")
    tt = sum(1 for r in results if r.get("tiktok") == "yes")
    print(f"\nDone -> {args.output}", file=sys.stderr)
    print(f"  reachable:   {ok}/{len(results)}", file=sys.stderr)
    print(f"  TikTok found: {tt}", file=sys.stderr)
    print(f"  qualifying:   {qual}", file=sys.stderr)

    if args.sheet:
        # Imported here so the script still runs for CSV-only use without gspread.
        try:
            import sheets
        except SystemExit as e:
            print(f"\nSheet write skipped: {e}", file=sys.stderr)
            return
        try:
            book = sheets.connect(args.sheet_id)
            rows = []
            for r in results:
                row = dict(r)
                if args.agency:
                    row["agency_name"] = args.agency
                rows.append(row)
            updated, added = sheets.write(book, "Ad Tags", rows)
            print(f"\nSheet updated: {book.url}", file=sys.stderr)
            print(f"  {added} new rows, {updated} updated", file=sys.stderr)
        except Exception as e:
            print(f"\nSheet write failed ({type(e).__name__}): {e}", file=sys.stderr)
            print(f"Your CSV at {args.output} is unaffected.", file=sys.stderr)


if __name__ == "__main__":
    main()

# NOTES / KNOWN LIMITS
#
# 1. Server-side tracking is invisible here. Meta CAPI, TikTok Events API, and
#    server-side GTM all fire from the brand's backend. A brand running TikTok
#    purely server-side will read as clean. No client-side method fixes this;
#    MediaRadar is the arbiter.
#
# 2. Consent gating. Sites using a CMP may not inject any pixel until consent is
#    granted, especially on EU traffic. Running from a US IP mitigates this for
#    US brands but does not eliminate it.
#
# 3. Homepage-only scope. The GTM container parse largely compensates, since the
#    container lists tags regardless of firing conditions. Without it (--no-gtm)
#    expect to miss a meaningful share of checkout-scoped tags.
#
# 4. Pixel presence is not spend. A pixel proves setup, not an active campaign.
#    Absence is the higher-confidence direction, which is what we want here.
#
# 5. If you need certainty on a shortlist, add Playwright: load homepage ->
#    product/service page -> a conversion step, and capture outbound requests to
#    analytics.tiktok.com, facebook.com/tr, and googleads.g.doubleclick.net.
#    ~3-5s per site vs ~1s here, so use it on finalists only.
