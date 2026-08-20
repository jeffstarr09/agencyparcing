#!/usr/bin/env python3
"""
common.py - Shared plumbing for the TikTok-gap pipeline.

Everything that touches the open web goes through Fetcher, so scraping conduct
is enforced in one place rather than re-implemented per script:

  * robots.txt is consulted before every request and honoured by default
  * one request per second per host, enforced with a per-host lock
  * a real, descriptive User-Agent that names the tool and a contact address
  * every response is cached to disk, so a re-run never re-hits a site
  * broken TLS is retried unverified, the same way pixel_check.fetch does,
    because we only read markup

Failures are values, never silence. A Page always comes back; if the fetch
failed, page.ok is False and page.error says why. Callers are expected to
record that distinction rather than collapse it into a zero.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlunparse

import requests

requests.packages.urllib3.disable_warnings()

# A real, descriptive agent. Small businesses deserve to know who is knocking
# and to be able to mail a human about it. Override with --user-agent.
CONTACT = os.environ.get("CRAWLER_CONTACT", "jeffstarr09@gmail.com")
DEFAULT_UA = (
    "TikTokGapBot/1.0 (+agency research crawler; respects robots.txt; "
    f"contact: {CONTACT})"
)

TIMEOUT = 20
DEFAULT_CACHE = ".cache"
DEFAULT_DELAY = 1.0  # seconds between requests to the same host


# --------------------------------------------------------------------------
# Domain normalisation
# --------------------------------------------------------------------------

# Registrable suffixes that carry a second label. Not the full Public Suffix
# List - that would mean a network fetch or a new dependency. This covers the
# ccTLD shapes an agency roster actually contains; anything unlisted falls back
# to last-two-labels, which is right for .com/.io/.agency/.marketing and wrong
# only for exotic cases we would review by hand anyway.
MULTI_PART_TLDS = {
    "co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "sch.uk", "ac.uk", "gov.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "id.au",
    "co.nz", "net.nz", "org.nz", "govt.nz",
    "co.za", "org.za", "net.za",
    "com.br", "com.mx", "com.ar", "com.co", "com.pe", "com.sg", "com.my",
    "com.hk", "com.tw", "com.tr", "com.ua", "com.pl", "com.cn", "net.cn", "org.cn",
    "co.in", "net.in", "org.in", "co.il", "co.jp", "or.jp", "ne.jp", "co.kr",
    "co.th", "com.ph", "com.vn", "co.id",
}

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.I)
_HOST_OK_RE = re.compile(r"^[A-Za-z0-9.\-]+$")


def root_domain(raw):
    """
    Reduce anything domain-shaped to its registrable root, lowercased.

        https://WWW.Acme-Plumbing.com/services/?utm=x  -> acme-plumbing.com
        blog.acme.co.uk                                -> acme.co.uk
        not a domain                                   -> None

    This is the dedupe key for agencies and clients alike. Every comparison in
    the pipeline goes through here so that www/trailing-slash/http-vs-https
    variants collapse to one row.
    """
    raw = (raw or "").strip().strip("<>\"'")
    if not raw:
        return None
    if not _SCHEME_RE.match(raw):
        raw = "//" + raw
    host = urlparse(raw, scheme="https").netloc or ""
    host = host.split("@")[-1].split(":")[0].strip().rstrip(".").lower()
    if not host or "." not in host or not _HOST_OK_RE.match(host):
        return None
    if not re.search(r"\.[a-z]{2,}$", host):
        return None
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in MULTI_PART_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def base_url(raw, scheme="https"):
    """
    Root domain as a fetchable origin: acme.com -> https://acme.com

    An explicit scheme in the input is preserved. A handful of small agency and
    contractor sites are still http-only, and silently upgrading those to https
    turns a reachable site into a fetch_failed row.
    """
    rd = root_domain(raw)
    if not rd:
        return None
    m = _SCHEME_RE.match((raw or "").strip())
    if m:
        scheme = m.group(0)[:-3].lower()
    return f"{scheme}://{rd}"


def canonical_url(url):
    """Drop the fragment and any trailing slash so a URL set dedupes properly."""
    p = urlparse(url)
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", p.query, ""))


def url_key(url):
    """
    Scheme-insensitive identity for a page.

    A sitemap that lists http:// URLs while the canonical paths are built as
    https:// otherwise makes the crawler fetch every page twice - double the
    requests at the site, and duplicate evidence in the output.
    """
    p = urlparse(canonical_url(url))
    return f"{p.netloc.lower().removeprefix('www.')}{p.path}{'?' + p.query if p.query else ''}"


def dedupe_urls(urls):
    """First occurrence wins, comparing on url_key rather than the raw string."""
    seen, out = set(), []
    for u in urls:
        k = url_key(u)
        if k not in seen:
            seen.add(k)
            out.append(u)
    return out


def same_site(url, domain):
    """True when url belongs to domain or one of its subdomains."""
    rd = root_domain(url)
    return bool(rd) and rd == root_domain(domain)


def now_stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

@dataclass
class Page:
    """One fetch attempt. Always returned - never raised, never None."""
    url: str
    status: int = 0
    text: str = ""
    error: str = ""
    from_cache: bool = False
    final_url: str = ""
    content_type: str = ""

    @property
    def ok(self):
        return self.status == 200 and not self.error and bool(self.text)

    @property
    def failure(self):
        """A short, recordable reason. Empty string when the fetch was fine."""
        if self.error:
            return self.error
        if self.status == 0:
            return "no_response"
        if self.status != 200:
            return f"http_{self.status}"
        if not self.text:
            return "empty_body"
        return ""


class Fetcher:
    """
    Polite, cached HTTP GET.

    Not thread-safe by accident - it is thread-safe on purpose: the rate limiter
    and the robots cache are both lock-guarded, so a ThreadPoolExecutor can share
    one Fetcher and still honour one-request-per-second-per-host.
    """

    def __init__(self, cache_dir=DEFAULT_CACHE, delay=DEFAULT_DELAY,
                 user_agent=DEFAULT_UA, obey_robots=True, use_cache=True,
                 timeout=TIMEOUT, verbose=True):
        self.cache_dir = cache_dir
        self.delay = float(delay)
        self.user_agent = user_agent
        self.obey_robots = obey_robots
        self.use_cache = use_cache
        self.timeout = timeout
        self.verbose = verbose

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self._host_lock = threading.Lock()
        self._last_hit = {}       # host -> monotonic timestamp
        self._host_locks = {}     # host -> Lock
        self._robots = {}         # host -> RobotFileParser | None
        self._robots_lock = threading.Lock()
        self.stats = {"fetched": 0, "cached": 0, "failed": 0, "robots_blocked": 0}

        if self.use_cache:
            os.makedirs(os.path.join(self.cache_dir, "pages"), exist_ok=True)
        if not obey_robots:
            print("WARNING: --ignore-robots is on. robots.txt will not be honoured.",
                  file=sys.stderr)

    # -- cache -------------------------------------------------------------

    def _cache_path(self, url):
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, "pages", f"{digest}.json")

    def _cache_read(self, url):
        if not self.use_cache:
            return None
        path = self._cache_path(url)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                blob = json.load(f)
        except (OSError, ValueError):
            # A truncated cache entry is a bug we can recover from, but not one
            # we hide: drop it and re-fetch.
            try:
                os.remove(path)
            except OSError:
                pass
            return None
        return Page(url=url, status=blob.get("status", 0), text=blob.get("text", ""),
                    error=blob.get("error", ""), from_cache=True,
                    final_url=blob.get("final_url", url),
                    content_type=blob.get("content_type", ""))

    def _cache_write(self, page):
        if not self.use_cache:
            return
        try:
            with open(self._cache_path(page.url), "w", encoding="utf-8") as f:
                json.dump({
                    "url": page.url, "status": page.status, "text": page.text,
                    "error": page.error, "final_url": page.final_url,
                    "content_type": page.content_type,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }, f)
        except OSError as e:
            print(f"  cache write failed for {page.url}: {e}", file=sys.stderr)

    # -- robots ------------------------------------------------------------

    def robots_for(self, url):
        """Fetch and memoise robots.txt for a host. None means 'no usable file'."""
        host = urlparse(url).netloc.lower()
        with self._robots_lock:
            if host in self._robots:
                return self._robots[host]
        robots_url = f"{urlparse(url).scheme}://{host}/robots.txt"
        parser = None
        self._throttle(host)
        # Through _raw_get, so the https-to-http fallback applies here too. A
        # robots.txt that is only served over http would otherwise look absent,
        # and the crawler would quietly ignore rules the site does publish.
        page = self._raw_get(robots_url)
        if page.status == 200 and page.text:
            parser = urllib.robotparser.RobotFileParser()
            try:
                parser.parse(page.text.splitlines())
            except Exception:
                parser = None
        with self._robots_lock:
            self._robots[host] = parser
        return parser

    def allowed(self, url):
        if not self.obey_robots:
            return True
        parser = self.robots_for(url)
        if parser is None:
            return True
        try:
            return parser.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def sitemaps_from_robots(self, url):
        parser = self.robots_for(url)
        if parser is None:
            return []
        return list(getattr(parser, "sitemaps", None) or [])

    # -- rate limiting -----------------------------------------------------

    def _throttle(self, host):
        if self.delay <= 0:
            return
        with self._host_lock:
            lock = self._host_locks.setdefault(host, threading.Lock())
        with lock:
            last = self._last_hit.get(host)
            if last is not None:
                wait = self.delay - (time.monotonic() - last)
                if wait > 0:
                    time.sleep(wait)
            self._last_hit[host] = time.monotonic()

    # -- the actual GET ----------------------------------------------------

    def get(self, url, allow_robots_override=False):
        """
        GET a URL through the cache, the robots check and the rate limiter.

        Returns a Page. A blocked or failed fetch is a Page with .ok False and
        a populated .error - callers must record it, not discard it.
        """
        url = canonical_url(url)
        cached = self._cache_read(url)
        if cached is not None:
            self.stats["cached"] += 1
            return cached

        if not allow_robots_override and not self.allowed(url):
            self.stats["robots_blocked"] += 1
            page = Page(url=url, error="robots_disallowed")
            self._cache_write(page)  # cache the refusal too; don't re-ask
            return page

        host = urlparse(url).netloc.lower()
        self._throttle(host)
        page = self._raw_get(url)
        if page.ok or page.status:
            self.stats["fetched"] += 1
        else:
            self.stats["failed"] += 1
        self._cache_write(page)
        if self.verbose:
            marker = "ok " if page.ok else "!! "
            print(f"    {marker}{page.status or '---'} {url}"
                  + (f"  ({page.error})" if page.error else ""), file=sys.stderr)
        return page

    def _raw_get(self, url):
        page = self._attempt(url)
        # A share of small contractor and med-spa sites still have no working
        # https listener at all. One http retry turns those from fetch_failed
        # into a real read; anything that failed for another reason is returned
        # as it was, so the original error is what gets recorded.
        if (not page.ok and url.startswith("https://")
                and page.error.startswith(("fetch_failed:ConnectionError",
                                           "fetch_failed:SSLError",
                                           "fetch_failed:ConnectTimeout"))):
            retry = self._attempt("http://" + url[len("https://"):])
            if retry.ok:
                retry.url = url
                retry.error = ""
                return retry
        return page

    def _attempt(self, url):
        for verify in (True, False):
            try:
                r = self._session.get(url, timeout=self.timeout,
                                      allow_redirects=True, verify=verify)
                ctype = r.headers.get("Content-Type", "")
                _fix_encoding(r, ctype)
                body = r.text if _is_texty(ctype) else ""
                return Page(url=url, status=r.status_code, text=body,
                            final_url=r.url, content_type=ctype)
            except requests.exceptions.SSLError:
                # Plenty of contractor and med-spa sites have broken certs.
                # Retry unverified rather than losing the row - markup only.
                continue
            except requests.exceptions.RequestException as e:
                return Page(url=url, error=f"fetch_failed:{type(e).__name__}")
            except Exception as e:  # noqa: BLE001 - surfaced, not swallowed
                return Page(url=url, error=f"error:{type(e).__name__}")
        return Page(url=url, error="fetch_failed:SSLError")


_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.I)


def _fix_encoding(response, content_type):
    """
    Decide the character encoding the way a browser does.

    requests defaults text/* to ISO-8859-1 whenever the server omits a charset,
    which is most small-business sites. That silently turns every em-dash, curly
    quote and accented letter into mojibake - and a case-study heading like
    "Acme - 6x ROAS" stops matching its pattern, so the client is never found.
    The bug shows up as missing clients, not as an error, which is exactly the
    kind of quiet failure worth spending a few lines to prevent.
    """
    if "charset=" in (content_type or "").lower():
        return
    m = _META_CHARSET_RE.search(response.content[:4096])
    if m:
        try:
            response.encoding = m.group(1).decode("ascii")
            return
        except (UnicodeDecodeError, LookupError):
            pass
    response.encoding = response.apparent_encoding or "utf-8"


def _is_texty(content_type):
    ct = (content_type or "").lower()
    if not ct:
        return True
    return any(t in ct for t in ("html", "xml", "text", "json", "javascript"))


# --------------------------------------------------------------------------
# Sitemaps
# --------------------------------------------------------------------------

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def sitemap_urls(fetcher, domain, max_urls=3000, max_indexes=8):
    """
    Collect every URL a site advertises: /sitemap.xml, whatever robots.txt
    points at, and one level of sitemap-index expansion.

    Returns (urls, note). note is a short human-readable string explaining what
    happened - "sitemap:412 urls", "no_sitemap" - so the caller can record how
    the page list was arrived at instead of guessing later.
    """
    origin = base_url(domain)
    if not origin:
        return [], "bad_domain"

    candidates = [f"{origin}/sitemap.xml", f"{origin}/sitemap_index.xml"]
    for sm in fetcher.sitemaps_from_robots(origin):
        if sm not in candidates:
            candidates.insert(0, sm)

    seen, out, indexes_expanded = set(), [], 0
    queue = list(candidates)
    while queue and len(out) < max_urls:
        sm_url = queue.pop(0)
        if sm_url in seen:
            continue
        seen.add(sm_url)
        page = fetcher.get(sm_url)
        if not page.ok or "<loc" not in page.text.lower():
            continue
        locs = _LOC_RE.findall(page.text)
        is_index = "<sitemapindex" in page.text.lower()
        if is_index and indexes_expanded < max_indexes:
            indexes_expanded += 1
            for loc in locs[:max_indexes * 4]:
                if loc not in seen:
                    queue.append(loc)
            continue
        for loc in locs:
            cu = canonical_url(loc)
            if same_site(cu, domain) and cu not in out:
                out.append(cu)
            if len(out) >= max_urls:
                break

    if out:
        return out, f"sitemap:{len(out)} urls"
    return [], "no_sitemap"


def nav_links(html, page_url, domain):
    """
    Same-site links off a page, canonicalised and deduped. The fallback when a
    site has no sitemap: the nav is the site's own table of contents.
    """
    out = []
    for href in re.findall(r'<a\b[^>]*?href\s*=\s*["\']([^"\'>]+)["\']', html or "", re.I):
        href = href.strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        absolute = canonical_url(urljoin(page_url, href))
        if same_site(absolute, domain) and absolute not in out:
            out.append(absolute)
    return out


# --------------------------------------------------------------------------
# Output: local file first, remote second
# --------------------------------------------------------------------------

def write_csv(path, fieldnames, rows, quiet=False):
    """Local write. This happens before any network write, always."""
    if not path:
        return
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fieldnames})
    if not quiet:
        print(f"\nWrote {len(rows)} rows -> {path}", file=sys.stderr)


def push_sheet(tab, rows, sheet_id=None, local_path=None):
    """
    Second-stage write to Google Sheets. Mirrors pixel_check.main: the local
    file has already landed, so a Sheets failure is reported loudly and the run
    still leaves usable output behind.
    """
    if not rows:
        print("Nothing to write to Sheets.", file=sys.stderr)
        return
    try:
        import sheets as sheets_mod
    except SystemExit as e:
        print(f"\nSheet write skipped: {e}", file=sys.stderr)
        return
    try:
        book = sheets_mod.connect(sheet_id)
        updated, added = sheets_mod.write(book, tab, rows)
        print(f"\nSheet updated: {book.url}", file=sys.stderr)
        print(f"  '{tab}': {added} new rows, {updated} updated", file=sys.stderr)
    except SystemExit as e:
        # sheets.connect exits loudly on missing creds / permissions. Report it
        # without losing the fact that the local file is already safe.
        print(f"\nSheet write failed: {e}", file=sys.stderr)
        if local_path:
            print(f"Your CSV at {local_path} is unaffected.", file=sys.stderr)
    except Exception as e:
        print(f"\nSheet write failed ({type(e).__name__}): {e}", file=sys.stderr)
        if local_path:
            print(f"Your CSV at {local_path} is unaffected.", file=sys.stderr)


def read_domain_list(source, column="domain"):
    """
    Accept a CSV path, a .txt path, or a bare domain / comma-separated list.

    Every script in the pipeline takes input this way, so you can spot-check a
    single agency without building a file for it.
    """
    if not source:
        return []
    if os.path.exists(source):
        if source.lower().endswith((".csv", ".tsv")):
            delim = "\t" if source.lower().endswith(".tsv") else ","
            with open(source, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f, delimiter=delim)
                fields = reader.fieldnames or []
                if column not in fields:
                    # Be forgiving about which column holds the domain, but say
                    # what we picked rather than guessing in silence.
                    for alt in ("agency_domain", "client_domain", "domain", "url", "website"):
                        if alt in fields:
                            print(f"Column '{column}' not found; using '{alt}'.",
                                  file=sys.stderr)
                            column = alt
                            break
                    else:
                        sys.exit(f"Column '{column}' not found in {source}. "
                                 f"Available: {fields}")
                return [r[column].strip() for r in reader if (r.get(column) or "").strip()]
        with open(source, encoding="utf-8-sig") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    return [p.strip() for p in re.split(r"[,\s]+", source) if p.strip()]


def add_crawl_args(ap):
    """The scraping-conduct flags, identical across every script."""
    g = ap.add_argument_group("crawling")
    g.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                   help="seconds between requests to the same host (default 1.0)")
    g.add_argument("--cache-dir", default=DEFAULT_CACHE,
                   help="where fetched pages are cached (default .cache)")
    g.add_argument("--no-cache", action="store_true",
                   help="bypass the page cache and re-fetch everything")
    g.add_argument("--ignore-robots", action="store_true",
                   help="do not honour robots.txt (off by default; be careful)")
    g.add_argument("--user-agent", default=DEFAULT_UA,
                   help="override the crawler User-Agent")
    g.add_argument("-w", "--workers", type=int, default=6,
                   help="parallel workers, across hosts (default 6)")
    g.add_argument("--quiet", action="store_true", help="suppress per-URL fetch logging")
    return ap


def add_sheet_args(ap):
    """The output flags, identical across every script."""
    g = ap.add_argument_group("output")
    g.add_argument("--sheet", action="store_true",
                   help="also write results to Google Sheets")
    g.add_argument("--sheet-id", default=None, help="override the target workbook id")
    g.add_argument("--dry-run", action="store_true",
                   help="parse and print, write nothing anywhere")
    return ap


def fetcher_from_args(args):
    return Fetcher(
        cache_dir=args.cache_dir,
        delay=args.delay,
        user_agent=args.user_agent,
        obey_robots=not args.ignore_robots,
        use_cache=not args.no_cache,
        verbose=not args.quiet,
    )


def report_stats(fetcher):
    s = fetcher.stats
    print(f"\nFetch stats: {s['fetched']} fetched, {s['cached']} from cache, "
          f"{s['failed']} failed, {s['robots_blocked']} blocked by robots.txt",
          file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Smoke-test the shared helpers.")
    ap.add_argument("domain", nargs="?", default="example.com")
    add_crawl_args(ap)
    args = ap.parse_args()

    print("root_domain checks:")
    for probe in ["https://WWW.Acme-Plumbing.com/services/?x=1", "blog.acme.co.uk",
                  "http://acme.com", "Acme Plumbing", "acme.com."]:
        print(f"  {probe!r:50} -> {root_domain(probe)}")

    f = fetcher_from_args(args)
    urls, note = sitemap_urls(f, args.domain, max_urls=20)
    print(f"\nsitemap for {args.domain}: {note}")
    for u in urls[:20]:
        print("  ", u)
    report_stats(f)
