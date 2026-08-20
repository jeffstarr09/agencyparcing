#!/usr/bin/env python3
"""
find_agencies.py - Build a candidate list of small-to-midsized agencies.

Takes a vertical and optionally a geography, works the agency directories and
search, drops anything on the TikTok Marketing Partner suppression list,
dedupes by root domain, and writes candidates to the Agencies tab.

    python find_agencies.py --vertical home_services --geo "Phoenix, AZ"
    python find_agencies.py --vertical local_health --geo-file data/metros.txt --sheet
    python find_agencies.py --vertical dtc --source search --limit 50
    python find_agencies.py --seed data/my_agencies.csv --vertical home_services
    python find_agencies.py --from-html saved/clutch-hvac-*.html --vertical home_services

Sources, in rough order of value:

  directories  Clutch, Sortlist, DesignRush, UpCity, Agency Spotter. Their
               service-and-location taxonomies do most of the filtering work.
  search       "<term> marketing agency <metro>" patterns, excluding the
               directories so you get the agencies' own sites.
  google_partners  The public Google Partners directory.
  seed         A CSV you already have.
  from_html    Saved listing pages. Cloudflare turns the directories away from
               datacenter IPs and sometimes from residential ones too; saving
               the page from your own browser is the reliable path when it does.

On failure this records the failure. A source that gets a 403 is written out as
a failed source, never as "found nothing" - the difference between "no HVAC
agencies in Tulsa" and "Clutch blocked us" is the whole ballgame.
"""

import argparse
import re
import sys
from urllib.parse import quote_plus, urljoin

import common
import tiktok_partners

# --------------------------------------------------------------------------
# Taxonomy
# --------------------------------------------------------------------------

VERTICALS = {
    "home_services": [
        "hvac", "roofing", "plumbing", "solar", "remodeling", "home remodeling",
        "garage door", "pest control", "landscaping", "home services",
        "contractor",
    ],
    "local_health": [
        "med spa", "medspa", "dental", "dentist", "dermatology", "cosmetic surgery",
        "plastic surgery", "vision", "optometry", "fitness", "gym", "chiropractic",
        "healthcare",
    ],
    "dtc": [
        "dtc", "direct to consumer", "ecommerce", "e-commerce", "shopify",
        "consumer brand",
    ],
}

# Where the small-shop demand actually is. Trimmed to metros with enough local
# service advertisers to support agencies of the target size.
DEFAULT_METROS = [
    "Phoenix AZ", "Dallas TX", "Houston TX", "Austin TX", "Atlanta GA",
    "Charlotte NC", "Nashville TN", "Tampa FL", "Orlando FL", "Denver CO",
    "Las Vegas NV", "Salt Lake City UT", "Kansas City MO", "Columbus OH",
    "Indianapolis IN", "Minneapolis MN", "St Louis MO", "San Diego CA",
    "Sacramento CA", "Portland OR", "Seattle WA", "Raleigh NC", "Jacksonville FL",
    "Oklahoma City OK", "San Antonio TX",
]

# Holding companies, global networks, and consultancies. Not the target profile,
# and they pollute directory listings because they buy placement.
NETWORK_NAMES = {
    "wpp", "omnicom", "publicis", "dentsu", "interpublic", "ipg", "havas",
    "accenture", "accenture song", "deloitte", "deloitte digital", "pwc", "ey",
    "kpmg", "mccann", "ogilvy", "vmly&r", "vml", "wunderman", "grey", "ddb",
    "bbdo", "tbwa", "leo burnett", "saatchi", "razorfish", "sapient",
    "publicis sapient", "digitas", "starcom", "zenith", "mindshare", "mediacom",
    "wavemaker", "essence", "initiative", "ubm", "isobar", "merkle", "iprospect",
    "horizon media", "group m", "groupm", "hearts & science", "carat",
}

# Domains that show up on every directory listing and are never an agency.
NON_AGENCY_DOMAINS = {
    "clutch.co", "sortlist.com", "designrush.com", "upcity.com", "agencyspotter.com",
    "google.com", "facebook.com", "instagram.com", "linkedin.com", "twitter.com",
    "x.com", "youtube.com", "tiktok.com", "pinterest.com", "vimeo.com",
    "wordpress.org", "wordpress.com", "wix.com", "squarespace.com", "godaddy.com",
    "hubspot.com", "shopify.com", "mailchimp.com", "semrush.com", "ahrefs.com",
    "gstatic.com", "googleapis.com", "cloudflare.com", "amazonaws.com",
    "bing.com", "duckduckgo.com", "yelp.com", "glassdoor.com", "indeed.com",
    "crunchbase.com", "medium.com", "github.com", "apple.com", "microsoft.com",
    "adobe.com", "salesforce.com", "trustpilot.com", "g2.com", "capterra.com",
    "partners.tiktok.com", "business.tiktok.com", "goodfirms.co", "themanifest.com",
    "expertise.com", "agencyvista.com", "wadline.com",
}

# --------------------------------------------------------------------------
# Directory sources
#
# One dict per directory. When a selector breaks - and it will, these sites get
# redesigned - this is the only place to edit. `listing` is a URL template with
# {term} and {geo} slots; {geo_slug} is the same geography as a URL segment.
# --------------------------------------------------------------------------

DIRECTORIES = {
    "clutch": {
        "name": "Clutch",
        "listing": "https://clutch.co/agencies/digital-marketing?keyword={term}&location={geo}",
        "profile_link": re.compile(r'href="(/profile/[^"#?]+)"', re.I),
        "base": "https://clutch.co",
        # Clutch masks the outbound website link behind a visit-website tracker.
        "website": [
            re.compile(r'href="https?://clutch\.co/go/[^"]*?u=([^"&]+)"', re.I),
            re.compile(r'class="[^"]*website-link[^"]*"[^>]*>\s*<a[^>]+href="([^"]+)"', re.I),
            re.compile(r'<a[^>]+href="([^"]+)"[^>]*>\s*Visit [Ww]ebsite', re.I),
        ],
    },
    "sortlist": {
        "name": "Sortlist",
        "listing": "https://www.sortlist.com/s/{term}-agencies/{geo_slug}",
        "profile_link": re.compile(r'href="(/agency/[^"#?]+)"', re.I),
        "base": "https://www.sortlist.com",
        "website": [re.compile(r'<a[^>]+href="(https?://(?!www\.sortlist)[^"]+)"[^>]*>\s*(?:Visit|Website)', re.I)],
    },
    "designrush": {
        "name": "DesignRush",
        "listing": "https://www.designrush.com/agency/digital-marketing/{geo_slug}?keyword={term}",
        "profile_link": re.compile(r'href="(/agency/profile/[^"#?]+)"', re.I),
        "base": "https://www.designrush.com",
        "website": [re.compile(r'<a[^>]+href="([^"]+)"[^>]*(?:rel="[^"]*nofollow[^"]*")?[^>]*>\s*Visit [Ww]ebsite', re.I)],
    },
    "upcity": {
        "name": "UpCity",
        "listing": "https://upcity.com/marketing-agencies/{geo_slug}?q={term}",
        "profile_link": re.compile(r'href="(/profiles/[^"#?]+)"', re.I),
        "base": "https://upcity.com",
        "website": [re.compile(r'<a[^>]+href="([^"]+)"[^>]*>\s*(?:Visit )?[Ww]ebsite', re.I)],
    },
    "agencyspotter": {
        "name": "Agency Spotter",
        "listing": "https://www.agencyspotter.com/search?q={term}%20{geo}",
        "profile_link": re.compile(r'href="(/[a-z0-9\-]+/?)"\s*class="[^"]*agency', re.I),
        "base": "https://www.agencyspotter.com",
        "website": [re.compile(r'<a[^>]+href="(https?://(?!www\.agencyspotter)[^"]+)"[^>]*>\s*(?:Visit|Website)', re.I)],
    },
    "google_partners": {
        "name": "Google Partners",
        "listing": "https://www.google.com/partners/agency-search?q={term}%20{geo}",
        "profile_link": re.compile(r'href="(/partners/agency\?id=[^"#]+)"', re.I),
        "base": "https://www.google.com",
        "website": [re.compile(r'<a[^>]+href="(https?://(?!\w+\.google\.com)[^"]+)"[^>]*>\s*(?:Visit|Website)', re.I)],
    },
}

SEARCH_ENGINES = {
    # DuckDuckGo's HTML endpoint is the one that answers a scripted GET without
    # an API key. Google's does too, sometimes, and returns a consent wall the
    # rest of the time - which is recorded as a failure, not as zero results.
    "duckduckgo": "https://html.duckduckgo.com/html/?q={q}",
    "google": "https://www.google.com/search?q={q}&num=30",
    "bing": "https://www.bing.com/search?q={q}&count=30",
}

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_OG_SITE_RE = re.compile(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']', re.I)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
_EMPLOYEE_RE = re.compile(
    r"(\d{1,3})\s*(?:-|–|—|to)\s*(\d{1,4})\s*(?:employees|people|staff|team members)", re.I)
_EMPLOYEE_SINGLE_RE = re.compile(r"\b(\d{1,4})\+?\s*(?:employees|people on staff)\b", re.I)


def _text(html):
    html = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", html or "")
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _clean_name(raw):
    """Strip the boilerplate a <title> carries around an agency's actual name."""
    name = re.sub(r"&amp;", "&", re.sub(r"<[^>]+>", " ", raw or ""))
    name = " ".join(name.split())
    # "Acme Digital | Top HVAC Marketing Agency in Phoenix" -> "Acme Digital"
    name = re.split(r"\s*[|–—>·•]\s*|\s+-\s+", name)[0].strip()
    name = re.sub(r"^(?:home|welcome to|about)\s*[:\-]?\s*", "", name, flags=re.I).strip()
    return name[:80]


def _slug(geo):
    return re.sub(r"[^a-z0-9]+", "-", (geo or "").lower()).strip("-")


def _is_network(name):
    n = (name or "").strip().lower()
    if not n:
        return False
    if n in NETWORK_NAMES:
        return True
    return any(re.search(rf"\b{re.escape(w)}\b", n) for w in NETWORK_NAMES if len(w) > 5)


def _plausible_agency_domain(domain):
    rd = common.root_domain(domain)
    if not rd or rd in NON_AGENCY_DOMAINS:
        return None
    # Directory and aggregator shapes we never want as a candidate row.
    if any(rd.endswith(sfx) for sfx in (".gov", ".edu", ".mil")):
        return None
    if re.search(r"(?:cdn|static|assets|img|images|fonts|track|analytics)\.", rd):
        return None
    return rd


# --------------------------------------------------------------------------
# Candidate records
# --------------------------------------------------------------------------

def _candidate(domain, name="", vertical="", geo="", source="", notes=""):
    return {
        "agency_name": name,
        "agency_domain": domain,
        "vertical": vertical,
        "hq_location": geo,
        "employee_count": "",
        "mentions_tiktok": "",       # check_agency_tiktok.py fills this
        "tiktok_evidence": "",
        "client_page_url": "",       # parse_clients.py fills this
        "clients_found": "",
        "status": "candidate",
        "notes": notes,
        "_source": source,           # underscore fields are local-only
    }


# --------------------------------------------------------------------------
# Source: directories
# --------------------------------------------------------------------------

def from_directory(fetcher, key, term, geo, vertical, max_profiles, failures):
    cfg = DIRECTORIES[key]
    url = cfg["listing"].format(term=quote_plus(term), geo=quote_plus(geo or ""),
                                geo_slug=_slug(geo) or "united-states")
    page = fetcher.get(url)
    if not page.ok:
        failures.append({"source": cfg["name"], "url": url, "reason": page.failure,
                         "term": term, "geo": geo})
        return []

    if _looks_like_a_wall(page.text):
        failures.append({"source": cfg["name"], "url": url,
                         "reason": "bot_challenge", "term": term, "geo": geo})
        return []

    out = []
    profile_paths = list(dict.fromkeys(cfg["profile_link"].findall(page.text)))
    if not profile_paths:
        failures.append({"source": cfg["name"], "url": url,
                         "reason": "no_profile_links_matched", "term": term, "geo": geo})

    for path in profile_paths[:max_profiles]:
        prof_url = urljoin(cfg["base"], path)
        prof = fetcher.get(prof_url)
        if not prof.ok:
            failures.append({"source": cfg["name"], "url": prof_url,
                             "reason": prof.failure, "term": term, "geo": geo})
            continue
        rec = _from_profile(prof.text, prof_url, cfg, term, geo, vertical)
        if rec:
            out.append(rec)

    # Some listings render the agency's own site inline. Harvest those too - the
    # profile fetch above is the expensive part and this costs nothing.
    for rd in _outbound_domains(page.text, cfg["base"]):
        if not any(r["agency_domain"] == rd for r in out):
            out.append(_candidate(rd, "", vertical, geo, f"{key}:listing",
                                  "domain from listing page; name not captured"))
    return out


def _from_profile(html, prof_url, cfg, term, geo, vertical):
    website = ""
    for rx in cfg["website"]:
        m = rx.search(html)
        if m:
            from urllib.parse import unquote
            website = unquote(m.group(1))
            break
    domain = _plausible_agency_domain(website)
    if not domain:
        # No website on the profile is a real outcome, not an error - but a row
        # with no domain cannot be checked or deduped, so it is dropped rather
        # than written as a half-record.
        return None

    name = ""
    m = _OG_SITE_RE.search(html) or _H1_RE.search(html) or _TITLE_RE.search(html)
    if m:
        name = _clean_name(m.group(1))
    if _is_network(name):
        return None

    text = _text(html)
    employees = ""
    me = _EMPLOYEE_RE.search(text) or _EMPLOYEE_SINGLE_RE.search(text)
    if me:
        employees = me.group(0).strip()

    return _candidate(domain, name, vertical, geo, f"{cfg['name']}:profile",
                      f"profile: {prof_url}") | {"employee_count": employees}


def _outbound_domains(html, base):
    base_rd = common.root_domain(base)
    out = []
    for href in re.findall(r'href=["\'](https?://[^"\'<>]+)["\']', html or "", re.I):
        rd = _plausible_agency_domain(href)
        if rd and rd != base_rd and rd not in out:
            out.append(rd)
    return out


_WALL_MARKERS = (
    "just a moment", "checking your browser", "cf-browser-verification",
    "enable javascript and cookies to continue", "attention required! | cloudflare",
    "px-captcha", "captcha-delivery", "unusual traffic from your computer network",
    "our systems have detected unusual traffic",
)


def _looks_like_a_wall(html):
    low = (html or "")[:6000].lower()
    return any(m in low for m in _WALL_MARKERS)


# --------------------------------------------------------------------------
# Source: search
# --------------------------------------------------------------------------

def search_queries(term, geo):
    geo = (geo or "").strip()
    excl = " -site:clutch.co -site:sortlist.com -site:designrush.com -site:upcity.com"
    base = f'"{term} marketing agency"'
    return [
        f"{base} {geo}{excl}".strip(),
        f'"{term} advertising agency" {geo}{excl}'.strip(),
        f'"{term} ppc agency" {geo}{excl}'.strip(),
    ]


def from_search(fetcher, engine, term, geo, vertical, max_results, failures):
    template = SEARCH_ENGINES[engine]
    out = []
    for q in search_queries(term, geo):
        url = template.format(q=quote_plus(q))
        page = fetcher.get(url)
        if not page.ok:
            failures.append({"source": f"search:{engine}", "url": url,
                             "reason": page.failure, "term": term, "geo": geo})
            continue
        if _looks_like_a_wall(page.text):
            failures.append({"source": f"search:{engine}", "url": url,
                             "reason": "bot_challenge", "term": term, "geo": geo})
            continue
        hits = _search_result_domains(page.text, engine)
        if not hits:
            failures.append({"source": f"search:{engine}", "url": url,
                             "reason": "no_results_parsed", "term": term, "geo": geo})
        for rd, label in hits[:max_results]:
            out.append(_candidate(rd, _clean_name(label), vertical, geo,
                                  f"search:{engine}", f"query: {q}"))
    return out


_DDG_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
_GENERIC_RESULT_RE = re.compile(
    r'<a[^>]+href="(https?://[^"]+)"[^>]*>\s*<h[23][^>]*>(.*?)</h[23]>', re.I | re.S)


def _search_result_domains(html, engine):
    from urllib.parse import parse_qs, unquote, urlparse as _up
    pairs = _DDG_RESULT_RE.findall(html) + _GENERIC_RESULT_RE.findall(html)
    if not pairs:
        pairs = [(h, "") for h in re.findall(r'href="(https?://[^"]+)"', html, re.I)]
    seen, out = set(), []
    for href, label in pairs:
        href = unquote(href)
        # DuckDuckGo wraps results in /l/?uddg=<encoded>; unwrap before parsing.
        if "/l/?" in href or "uddg=" in href:
            qs = parse_qs(_up(href).query)
            href = (qs.get("uddg") or [href])[0]
        rd = _plausible_agency_domain(href)
        if rd and rd not in seen:
            seen.add(rd)
            out.append((rd, label))
    return out


# --------------------------------------------------------------------------
# Source: saved HTML
# --------------------------------------------------------------------------

def from_saved_html(paths, vertical, geo, failures):
    """
    Parse listing pages saved from a browser. The directories challenge scripted
    requests; a saved page is the reliable way through, and it keeps the parsing
    logic identical to the live path.
    """
    import glob
    import os
    out = []
    files = []
    for pattern in paths:
        matched = glob.glob(pattern)
        if not matched:
            failures.append({"source": "from_html", "url": pattern,
                             "reason": "no_files_matched", "term": "", "geo": geo})
        files.extend(matched)
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                html = f.read()
        except OSError as e:
            failures.append({"source": "from_html", "url": path,
                             "reason": f"read_failed:{type(e).__name__}", "term": "", "geo": geo})
            continue
        base = next((cfg["base"] for cfg in DIRECTORIES.values()
                     if common.root_domain(cfg["base"]) in html), "https://example.com")
        found = _outbound_domains(html, base)
        if not found:
            failures.append({"source": "from_html", "url": path,
                             "reason": "no_domains_parsed", "term": "", "geo": geo})
        for rd in found:
            out.append(_candidate(rd, "", vertical, geo, f"from_html:{os.path.basename(path)}",
                                  f"parsed from saved page {path}"))
    return out


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

def dedupe(records):
    """One row per root domain. Earlier, richer records win; later ones fill blanks."""
    by_domain = {}
    order = []
    for r in records:
        rd = common.root_domain(r.get("agency_domain"))
        if not rd:
            continue
        r = dict(r, agency_domain=rd)
        if rd not in by_domain:
            by_domain[rd] = r
            order.append(rd)
            continue
        kept = by_domain[rd]
        for k, v in r.items():
            if v and not kept.get(k):
                kept[k] = v
        srcs = {s for s in (kept.get("_source", ""), r.get("_source", "")) if s}
        kept["_source"] = " + ".join(sorted(srcs))
    return [by_domain[d] for d in order]


def enrich_from_homepage(fetcher, rec):
    """
    Fill the name from the agency's own homepage when a directory didn't give
    one. Cheap, and it is the difference between a usable row and a bare domain.
    Never invents: a failed fetch leaves the name blank and says so in notes.
    """
    if rec.get("agency_name"):
        return rec
    origin = common.base_url(rec["agency_domain"])
    if not origin:
        return rec
    page = fetcher.get(origin)
    if not page.ok:
        rec["status"] = "unreachable"
        rec["notes"] = (rec.get("notes", "") + f" | homepage {page.failure}").strip(" |")
        return rec
    m = _OG_SITE_RE.search(page.text) or _TITLE_RE.search(page.text)
    if m:
        name = _clean_name(m.group(1))
        if _is_network(name):
            rec["status"] = "excluded_network"
            rec["notes"] = (rec.get("notes", "") + " | matches a holding-company name").strip(" |")
        rec["agency_name"] = name
    return rec


OUT_FIELDS = ["agency_name", "agency_domain", "vertical", "hq_location",
              "employee_count", "mentions_tiktok", "tiktok_evidence",
              "client_page_url", "clients_found", "status", "notes", "_source"]

FAIL_FIELDS = ["source", "term", "geo", "url", "reason"]


def main():
    ap = argparse.ArgumentParser(
        description="Discover candidate agencies and write them to the Agencies tab.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Verticals: " + ", ".join(VERTICALS))
    ap.add_argument("--vertical", choices=sorted(VERTICALS), required=True,
                    help="which vertical's agencies to look for")
    ap.add_argument("--geo", action="append", default=[],
                    help="a metro, repeatable. Omit for the default metro list.")
    ap.add_argument("--geo-file", help="a file of metros, one per line")
    ap.add_argument("--all-metros", action="store_true",
                    help=f"sweep the built-in metro list ({len(DEFAULT_METROS)} metros)")
    ap.add_argument("--term", action="append", default=[],
                    help="override the vertical's search terms, repeatable")
    ap.add_argument("--source", action="append", default=[],
                    choices=sorted(DIRECTORIES) + ["search", "all"],
                    help="which sources to work, repeatable (default: all)")
    ap.add_argument("--engine", default="duckduckgo", choices=sorted(SEARCH_ENGINES),
                    help="search backend for --source search (default duckduckgo)")
    ap.add_argument("--seed", help="a CSV or txt of agency domains you already have")
    ap.add_argument("--from-html", action="append", default=[],
                    help="glob of saved listing pages to parse instead of fetching")
    ap.add_argument("--max-profiles", type=int, default=15,
                    help="profile pages to open per directory listing (default 15)")
    ap.add_argument("--max-results", type=int, default=20,
                    help="results to take per search query (default 20)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N candidates")
    ap.add_argument("--no-suppression", action="store_true",
                    help="skip the TikTok partner suppression list (say why in your notes)")
    ap.add_argument("--no-enrich", action="store_true",
                    help="don't fetch agency homepages to fill in missing names")
    ap.add_argument("-o", "--output", default="agencies.csv")
    ap.add_argument("--failures-output", default="agency_source_failures.csv",
                    help="where blocked and empty sources are recorded")
    common.add_crawl_args(ap)
    common.add_sheet_args(ap)
    args = ap.parse_args()

    terms = args.term or VERTICALS[args.vertical]
    geos = list(args.geo)
    if args.geo_file:
        geos += common.read_domain_list(args.geo_file)
    if args.all_metros or not geos:
        geos = geos or DEFAULT_METROS
    sources = args.source or ["all"]
    if "all" in sources:
        sources = sorted(DIRECTORIES) + ["search"]

    partner_domains = set()
    if not args.no_suppression:
        partner_domains = tiktok_partners.domain_set(required=True)
    else:
        print("WARNING: running without TikTok partner suppression.", file=sys.stderr)

    fetcher = common.fetcher_from_args(args)
    records, failures = [], []

    if args.seed:
        for d in common.read_domain_list(args.seed, column="agency_domain"):
            rd = common.root_domain(d)
            if rd:
                records.append(_candidate(rd, "", args.vertical, "", "seed",
                                          f"seeded from {args.seed}"))
        print(f"Seeded {len(records)} domains from {args.seed}", file=sys.stderr)

    if args.from_html:
        records += from_saved_html(args.from_html, args.vertical, geos[0] if geos else "",
                                   failures)

    if not args.seed and not args.from_html:
        total = len(terms) * len(geos)
        print(f"Working {len(sources)} source(s) x {len(terms)} term(s) x "
              f"{len(geos)} geo(s) = {total * len(sources)} listing fetches",
              file=sys.stderr)
        for geo in geos:
            for term in terms:
                print(f"\n[{args.vertical}] {term} / {geo}", file=sys.stderr)
                for src in sources:
                    if src == "search":
                        records += from_search(fetcher, args.engine, term, geo,
                                               args.vertical, args.max_results, failures)
                    else:
                        records += from_directory(fetcher, src, term, geo, args.vertical,
                                                  args.max_profiles, failures)
                if args.limit and len(dedupe(records)) >= args.limit:
                    print(f"  hit --limit {args.limit}, stopping", file=sys.stderr)
                    break
            else:
                continue
            break

    found_raw = len(records)
    records = dedupe(records)

    suppressed = [r for r in records
                  if not args.no_suppression and r["agency_domain"] in partner_domains]
    for r in suppressed:
        r["status"] = "excluded_tiktok_partner"
        r["notes"] = (r.get("notes", "") + " | badged TikTok Marketing Partner").strip(" |")
    kept = [r for r in records if r["status"] != "excluded_tiktok_partner"]

    if args.limit:
        kept = kept[:args.limit]

    if not args.no_enrich and kept:
        print(f"\nFilling in names from {len(kept)} agency homepages...", file=sys.stderr)
        for r in kept:
            enrich_from_homepage(fetcher, r)

    kept = [r for r in kept if r["status"] != "excluded_network"]

    common.report_stats(fetcher)
    print(f"\n{found_raw} raw hits -> {len(records)} unique domains", file=sys.stderr)
    print(f"  {len(suppressed)} suppressed as TikTok Marketing Partners", file=sys.stderr)
    print(f"  {len(kept)} candidates kept", file=sys.stderr)
    if failures:
        by_reason = {}
        for f in failures:
            by_reason[f["reason"]] = by_reason.get(f["reason"], 0) + 1
        print(f"  {len(failures)} source failures - NOT the same as zero results:",
              file=sys.stderr)
        for reason, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
            print(f"      {n:4}  {reason}", file=sys.stderr)

    if args.dry_run:
        for r in kept[:40]:
            print(f"  {r['agency_domain']:38} {r['agency_name'][:40]:40} {r['_source']}")
        print(f"\n--dry-run: nothing written.", file=sys.stderr)
        return

    # Local file first, remote second - same ordering as pixel_check.
    common.write_csv(args.output, OUT_FIELDS, kept + suppressed)
    if failures:
        common.write_csv(args.failures_output, FAIL_FIELDS, failures)

    if args.sheet:
        common.push_sheet("Agencies", kept, args.sheet_id, args.output)


if __name__ == "__main__":
    main()
