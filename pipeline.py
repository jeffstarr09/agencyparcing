#!/usr/bin/env python3
"""
pipeline.py - The whole chain, as one long-running job.

The command-line scripts each do one step and stop. This runs all of them, in
order, over and over, for as long as you let it - which is what you actually
want when the job is "go find me agencies."

It is built to be interrupted. Every batch writes its results to disk before
starting the next one, and finished work is recorded in data/run_state.json, so
closing the app and reopening it tomorrow picks up where it left off instead of
re-crawling everything.

Work is one (vertical, metro) pair at a time:

    find agencies -> check who already sells TikTok -> parse their clients
    -> pixel-check those clients -> re-score every agency found so far

Scoring runs across everything accumulated, not just the current batch, so the
leaderboard is always current and always sorted by the number that matters.

This module has no CLI of its own - app.py drives it. It talks to the outside
world through two callables you pass in:

    on_event(kind, message, **data)   progress, one line at a time
    should_stop()                     checked between every unit of work
"""

import csv
import json
import os
import re
import threading
import time
import traceback
from datetime import datetime, timezone

import check_agency_tiktok
import common
import feedback
import find_agencies
import parse_clients
import pixel_check
import score_agencies
import tiktok_partners

STATE_PATH = os.path.join("data", "run_state.json")

AGENCIES_CSV = "agencies.csv"
CLIENTS_CSV = "clients.csv"
TAGS_CSV = "results.csv"
SCORECARD_CSV = "agency_scorecard.csv"
FAILURES_CSV = "agency_source_failures.csv"

AGENCY_FIELDS = [
    "agency_name", "agency_domain", "vertical", "hq_location", "employee_count",
    "mentions_tiktok", "tiktok_evidence", "client_page_url", "clients_found",
    "status", "notes", "last_checked",
]
CLIENT_FIELDS = ["client_name", "client_domain", "agency_name", "agency_domain",
                 "vertical", "source", "confidence", "last_checked"]
TAG_FIELDS = ["domain", "client_name", "agency_name", "resolved_url", "http_status",
              "status", "qualifies", "tiktok", "meta", "google_ads", "floodlight",
              "pinterest", "snapchat", "gtm_ids", "ga4_ids",
              "tiktok_evidence", "meta_evidence", "google_ads_evidence",
              "floodlight_evidence", "pinterest_evidence", "snapchat_evidence",
              "last_checked"]


class Stopped(Exception):
    """Raised internally when the user asks to stop. Not an error."""


# --------------------------------------------------------------------------
# Durable state
# --------------------------------------------------------------------------

def load_state(path=STATE_PATH):
    if not os.path.exists(path):
        return {"done_batches": [], "started_at": "", "totals": {}}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        # A corrupted state file costs re-crawling, not correctness. Say so and
        # carry on rather than refusing to run.
        return {"done_batches": [], "started_at": "", "totals": {},
                "note": "previous state file was unreadable and was reset"}


def save_state(state, path=STATE_PATH):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)          # atomic: never leaves a half-written state file


def reset_state(path=STATE_PATH):
    if os.path.exists(path):
        os.remove(path)


# --------------------------------------------------------------------------
# Accumulating CSV stores
# --------------------------------------------------------------------------

class Store:
    """
    An upsert-by-key CSV that is rewritten in full after every batch.

    Full rewrites are the right trade here: these files top out in the low tens
    of thousands of rows, and the alternative - appending and deduping later -
    means a crash leaves duplicates behind.
    """

    def __init__(self, path, fields, key):
        self.path, self.fields, self.key = path, fields, key
        self.rows = {}
        self.order = []
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    k = self._key_of(row)
                    if k:
                        if k not in self.rows:
                            self.order.append(k)
                        self.rows[k] = row
        except (OSError, ValueError):
            pass

    def _key_of(self, row):
        parts = [str(row.get(c, "") or "").strip().lower() for c in self.key]
        return "::".join(parts) if any(parts) else ""

    def put(self, row):
        with self._lock:
            k = self._key_of(row)
            if not k:
                return False
            if k not in self.rows:
                self.order.append(k)
                self.rows[k] = row
                return True
            # Merge: a later pass fills blanks without wiping what we already knew.
            existing = self.rows[k]
            for field, value in row.items():
                if value not in ("", None):
                    existing[field] = value
            return False

    def put_all(self, rows):
        return sum(1 for r in rows if self.put(r))

    def all(self):
        with self._lock:
            return [dict(self.rows[k]) for k in self.order]

    def flush(self):
        with self._lock:
            rows = [self.rows[k] for k in self.order]
        common.write_csv(self.path, self.fields, rows, quiet=True)

    def __len__(self):
        return len(self.order)


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "verticals": ["home_services"],
    "metros": [],                # empty means find_agencies.DEFAULT_METROS
    "sources": ["search"],       # search is the only one reliable from a laptop
    "keep_going": True,          # cycle until stopped
    "use_sheets": False,
    "sheet_id": "",
    "delay": 1.0,
    "workers": 4,
    "max_agencies_per_batch": 12,
    "skip_suppression": False,
    "engine": "duckduckgo",
}


def batches(config):
    """Every (vertical, metro) pair, ordered so early results span verticals."""
    metros = config.get("metros") or find_agencies.DEFAULT_METROS
    verticals = config.get("verticals") or ["home_services"]
    out = []
    for metro in metros:
        for vertical in verticals:
            out.append((vertical, metro))
    return out


def run(config, on_event, should_stop, state_path=STATE_PATH):
    """
    Run until the work is done or should_stop() returns True.

    Never raises for a bad site, a blocked directory, or a missing sheet - those
    are reported through on_event and the run continues. It raises only if it
    cannot write results at all, which is the one failure worth stopping for.
    """
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(config or {})

    state = load_state(state_path)
    state.setdefault("done_batches", [])
    if not state.get("started_at"):
        state["started_at"] = datetime.now(timezone.utc).isoformat()

    agencies = Store(AGENCIES_CSV, AGENCY_FIELDS, ["agency_domain"])
    clients = Store(CLIENTS_CSV, CLIENT_FIELDS, ["agency_domain", "client_name"])
    tags = Store(TAGS_CSV, TAG_FIELDS, ["domain"])

    on_event("info", f"Resuming with {len(agencies)} agencies, {len(clients)} clients, "
                     f"{len(tags)} sites already checked."
             if len(agencies) else "Starting a fresh run.")

    fetcher = common.Fetcher(
        delay=cfg["delay"],
        obey_robots=True,
        use_cache=True,
        verbose=False,
    )

    suppression = set()
    if not cfg["skip_suppression"]:
        try:
            suppression = tiktok_partners.domain_set(required=False)
            if suppression:
                on_event("info", f"Suppressing {len(suppression)} badged TikTok "
                                 f"Marketing Partners.")
            else:
                on_event("warn", "No TikTok partner suppression list cached. "
                                 "Running without it - see Setup.")
        except SystemExit as e:
            on_event("warn", f"Suppression list unavailable: {e}")

    todo = [b for b in batches(cfg) if list(b) not in state["done_batches"]
            and b not in [tuple(x) for x in state["done_batches"]]]
    total = len(batches(cfg))
    if not todo and cfg["keep_going"]:
        on_event("info", "Every area has been covered once. Going round again to "
                         "refresh and go deeper.")
        state["done_batches"] = []
        todo = batches(cfg)

    on_event("plan", f"{len(todo)} area(s) to work through.",
             total=total, done=total - len(todo))
    dirty = False   # is there unsaved work? avoids a redundant second save

    try:
        for index, (vertical, metro) in enumerate(todo, 1):
            if should_stop():
                raise Stopped()
            on_event("batch", f"{vertical.replace('_', ' ')} in {metro}",
                     batch_index=index, batch_total=len(todo))
            dirty = True

            _run_batch(cfg, vertical, metro, fetcher, suppression,
                       agencies, clients, tags, on_event, should_stop)

            state["done_batches"].append([vertical, metro])
            _persist(agencies, clients, tags, state, state_path, cfg, on_event)
            dirty = False

            if cfg["keep_going"] and index == len(todo):
                on_event("info", "Finished a full pass. Starting another.")
                state["done_batches"] = []
                todo = todo + batches(cfg)

    except Stopped:
        on_event("info", "Stopping - saving everything found so far.")
    except Exception as e:
        on_event("error", f"Unexpected error: {type(e).__name__}: {e}")
        on_event("debug", traceback.format_exc())
    finally:
        # Only if a batch was interrupted mid-flight; a completed batch already saved.
        if dirty:
            _persist(agencies, clients, tags, state, state_path, cfg, on_event)

    return summary(agencies, clients, tags)


def _run_batch(cfg, vertical, metro, fetcher, suppression,
               agencies, clients, tags, on_event, should_stop):
    # --- 1. discovery -----------------------------------------------------
    on_event("step", f"Looking for {vertical.replace('_', ' ')} agencies in {metro}")
    found, failures = [], []
    terms = find_agencies.VERTICALS.get(vertical, [vertical])[:4]
    for term in terms:
        if should_stop():
            raise Stopped()
        for source in cfg["sources"]:
            try:
                if source == "search":
                    found += find_agencies.from_search(
                        fetcher, cfg["engine"], term, metro, vertical,
                        cfg.get("max_results", 15), failures)
                elif source in find_agencies.DIRECTORIES:
                    found += find_agencies.from_directory(
                        fetcher, source, term, metro, vertical,
                        cfg.get("max_profiles", 8), failures)
            except Exception as e:
                failures.append({"source": source, "term": term, "geo": metro,
                                 "url": "", "reason": f"crashed:{type(e).__name__}"})

    fresh = find_agencies.dedupe(found)
    known = {a["agency_domain"] for a in agencies.all()}
    new = [a for a in fresh
           if a["agency_domain"] not in known
           and a["agency_domain"] not in suppression]
    suppressed = len(fresh) - len([a for a in fresh if a["agency_domain"] not in suppression])

    if failures:
        blocked = sum(1 for f in failures if f["reason"] in
                      ("bot_challenge", "no_results_parsed", "no_profile_links_matched"))
        if blocked:
            on_event("warn", f"{blocked} source lookup(s) were blocked or returned "
                             f"nothing parseable. That is not the same as 'no agencies "
                             f"here' - see Problems.")
        _append_failures(failures)

    on_event("found", f"{len(new)} new agencies "
                      f"({len(fresh)} seen, {len(fresh) - len(new) - suppressed} already known"
                      + (f", {suppressed} are TikTok partners" if suppressed else "") + ")",
             new_agencies=len(new))

    new = new[:cfg["max_agencies_per_batch"]]
    for a in new:
        a.pop("_source", None)
        agencies.put(a)

    if not new:
        return

    # --- 2. does the agency already sell TikTok? --------------------------
    on_event("step", f"Checking {len(new)} agencies for TikTok in their services")
    checked = []
    for i, agency in enumerate(new, 1):
        if should_stop():
            raise Stopped()
        try:
            row = check_agency_tiktok.check_agency(fetcher, agency, run_pixel=True)
        except Exception as e:
            row = dict(agency, status=f"crashed:{type(e).__name__}", mentions_tiktok="")
        for k in ("_pages_fetched", "_pixel_status", "_own_tiktok_pixel"):
            row.pop(k, None)
        agencies.put(row)
        checked.append(row)
        on_event("progress", f"  {row.get('agency_domain', '')}: "
                             f"{_tiktok_label(row)}",
                 step="tiktok", i=i, n=len(new))

    targets = [r for r in checked if r.get("mentions_tiktok") != "yes"]
    on_event("info", f"{len(targets)} of {len(checked)} do not sell TikTok.")

    # --- 3. who are their clients? ---------------------------------------
    to_parse = targets or checked
    on_event("step", f"Reading client lists for {len(to_parse)} agencies")
    batch_clients = []
    for i, agency in enumerate(to_parse, 1):
        if should_stop():
            raise Stopped()
        try:
            found_clients, arow = parse_clients.parse_agency(fetcher, agency)
        except Exception as e:
            found_clients, arow = [], dict(
                agency, status=f"crashed:{type(e).__name__}", clients_found="")
        for c in found_clients:
            c.pop("_methods", None)
            c.pop("_name_from_domain", None)
        agencies.put(arow)
        clients.put_all(found_clients)
        batch_clients += found_clients
        on_event("progress",
                 f"  {arow.get('agency_domain', '')}: "
                 f"{arow.get('clients_found') or '—'} clients "
                 f"({arow.get('status', '')})",
                 step="clients", i=i, n=len(to_parse))

    # --- 4. pixel-check the clients ---------------------------------------
    seen_domains = {t["domain"] for t in tags.all()}
    to_check = []
    for c in batch_clients:
        d = (c.get("client_domain") or "").strip()
        if d and d not in seen_domains and d not in {x["domain"] for x in to_check}:
            to_check.append({"domain": d, "client_name": c.get("client_name", ""),
                             "agency_name": c.get("agency_name", "")})
    if to_check:
        on_event("step", f"Checking {len(to_check)} client sites for a TikTok pixel")
        for i, item in enumerate(to_check, 1):
            if should_stop():
                raise Stopped()
            session = pixel_check.requests.Session()
            try:
                row = pixel_check.check(item["domain"], session)
            except Exception as e:
                row = {"domain": item["domain"], "status": f"crashed:{type(e).__name__}"}
            finally:
                session.close()
            row["client_name"] = item["client_name"]
            row["agency_name"] = item["agency_name"]
            row["last_checked"] = common.now_stamp()
            tags.put(row)
            on_event("progress", f"  {item['domain']}: {_pixel_label(row)}",
                     step="pixels", i=i, n=len(to_check))
            time.sleep(max(0.0, cfg["delay"] * 0.5))
    else:
        on_event("info", "No new client domains to pixel-check in this batch.")


def _tiktok_label(row):
    m = row.get("mentions_tiktok")
    if m == "yes":
        return "already sells TikTok"
    if m == "no":
        return "no TikTok — a target"
    return f"couldn't tell ({row.get('status', 'unknown')})"


def _pixel_label(row):
    if row.get("status") != "ok":
        return f"couldn't read ({row.get('status', 'unknown')})"
    if row.get("tiktok") == "yes":
        return "has TikTok"
    if row.get("qualifies") == "yes":
        return "no TikTok, running Meta/Google — qualifies"
    return "no TikTok, but no Meta/Google either"


def _append_failures(failures):
    fields = ["source", "term", "geo", "url", "reason"]
    existing = []
    if os.path.exists(FAILURES_CSV):
        try:
            with open(FAILURES_CSV, newline="", encoding="utf-8-sig") as f:
                existing = list(csv.DictReader(f))
        except (OSError, ValueError):
            existing = []
    common.write_csv(FAILURES_CSV, fields, existing + failures, quiet=True)


def _persist(agencies, clients, tags, state, state_path, cfg, on_event):
    """Local files first, then scoring, then the sheet. Same order as everywhere."""
    agencies.flush()
    clients.flush()
    tags.flush()

    scored = score_agencies.score(clients.all(), tags.all(), agencies.all())
    common.write_csv(SCORECARD_CSV, score_agencies.SCORE_FIELDS, scored, quiet=True)

    state["totals"] = summary(agencies, clients, tags)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state, state_path)

    if cfg.get("use_sheets"):
        try:
            import sheets as sheets_mod
            book = sheets_mod.connect(cfg.get("sheet_id") or None)
            sheets_mod.write(book, "Agencies", score_agencies.to_agency_rows(scored))
            sheets_mod.write(book, "Clients", clients.all())
            sheets_mod.write(book, "Ad Tags", tags.all())
            on_event("info", "Google Sheet updated.")
        except SystemExit as e:
            on_event("warn", f"Sheet not updated: {e}. Your CSV files are fine.")
        except Exception as e:
            on_event("warn", f"Sheet not updated ({type(e).__name__}: {e}). "
                             f"Your CSV files are fine.")


def summary(agencies, clients, tags):
    arows, crows, trows = agencies.all(), clients.all(), tags.all()
    qualifying = sum(1 for t in trows if t.get("qualifies") == "yes")
    has_tt = sum(1 for t in trows if t.get("tiktok") == "yes")
    unreachable = sum(1 for t in trows if t.get("status") not in ("ok", ""))
    targets = sum(1 for a in arows if a.get("mentions_tiktok") == "no")
    review = sum(1 for a in arows if a.get("status") == "needs_manual_review")
    return {
        "agencies": len(arows),
        "target_agencies": targets,
        "clients": len(crows),
        "clients_with_domain": sum(1 for c in crows if (c.get("client_domain") or "").strip()),
        "sites_checked": len(trows),
        "qualifying_clients": qualifying,
        "clients_with_tiktok": has_tt,
        "unreachable": unreachable,
        "needs_review": review,
    }


def totals_from_disk():
    """
    Recount from the CSV files rather than trusting the saved run state.

    The state file only updates while a run is in progress; the UI needs the
    numbers to be right after a restart, and after a run that was stopped
    mid-batch. Reading the files is cheap and can't drift.
    """
    return summary(
        Store(AGENCIES_CSV, AGENCY_FIELDS, ["agency_domain"]),
        Store(CLIENTS_CSV, CLIENT_FIELDS, ["agency_domain", "client_name"]),
        Store(TAGS_CSV, TAG_FIELDS, ["domain"]),
    )


def top_agencies(limit=50):
    """The leaderboard, read back off disk so the UI and the run can't disagree."""
    if not os.path.exists(SCORECARD_CSV):
        return []
    try:
        with open(SCORECARD_CSV, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except (OSError, ValueError):
        return []
    for r in rows:
        for k in ("clients_found", "clients_checked", "qualifying", "tiktok_free",
                  "has_tiktok", "unreachable", "unchecked", "coverage_pct"):
            try:
                r[k] = int(r.get(k) or 0)
            except (TypeError, ValueError):
                r[k] = 0
    rows.sort(key=lambda r: (-r["qualifying"], -r["tiktok_free"], -r["coverage_pct"]))
    return rows[:limit]


def clients_for_review(limit=200):
    """Rows worth a human glance: least trustworthy first."""
    if not os.path.exists(CLIENTS_CSV):
        return []
    try:
        with open(CLIENTS_CSV, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except (OSError, ValueError):
        return []
    reviewed = _reviewed_keys()
    rank = {"low": 0, "medium": 1, "high": 2}
    rows = [r for r in rows
            if (r.get("agency_domain", ""), r.get("client_name", "")) not in reviewed]
    rows.sort(key=lambda r: (rank.get(r.get("confidence", "low"), 0),
                             r.get("agency_domain", "")))
    return rows[:limit]


def _reviewed_keys(path="feedback_verdicts.csv"):
    if not os.path.exists(path):
        return set()
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return {(r.get("agency_domain", ""), r.get("entity_key", ""))
                    for r in csv.DictReader(f)}
    except (OSError, ValueError):
        return set()


def record_verdict(entity_key, agency_domain, verdict, correct_value="", method="",
                   path="feedback_verdicts.csv"):
    """
    Append one verdict and immediately fold it into the learned rules, so the
    effect of clicking a button is visible on the very next batch.
    """
    row = {
        "entity_type": "client", "entity_key": entity_key,
        "agency_domain": agency_domain, "verdict": verdict,
        "correct_value": correct_value, "method": method, "note": "via app",
        "reviewed_at": common.now_stamp(), "applied": "",
    }
    rows = []
    if os.path.exists(path):
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
        except (OSError, ValueError):
            rows = []
    rows = [r for r in rows if not (r.get("entity_key") == entity_key
                                    and r.get("agency_domain") == agency_domain)]
    rows.append(row)
    common.write_csv(path, feedback.FEEDBACK_FIELDS, rows, quiet=True)

    rules, report = feedback.learn(rows, feedback.load_rules())
    feedback.save_rules(rules)
    parse_clients.RULES = feedback.Rules(rules)
    return report


# --------------------------------------------------------------------------
# Importing agencies you found yourself
#
# Search engines forbid automated searching in their robots.txt and this app
# honours that, so it cannot run the searches for you. It can take the results
# once *you* have run one: paste the page, or a list of addresses, and this
# pulls the agency websites out.
# --------------------------------------------------------------------------

# Bare domains, and full URLs, out of arbitrary pasted text or saved HTML.
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]}]+", re.I)
_BARE_DOMAIN_RE = re.compile(
    r"\b(?:www\.)?([a-z0-9][a-z0-9\-]{0,61}"
    r"(?:\.[a-z0-9][a-z0-9\-]{0,61})*"
    r"\.(?:com|net|org|co|io|agency|marketing|digital|media|us|biz|studio|group))\b",
    re.I)

SEARCH_TEMPLATES = [
    ('"{term} marketing agency" {geo}', "the plain one"),
    ('"{term} advertising agency" {geo}', "catches shops that say advertising"),
    ('"{term} ppc agency" {geo}', "catches the paid-media specialists"),
]


def search_links(vertical, geo, engine="google"):
    """
    Ready-made searches for you to click. These open in your own browser, where
    you are simply a person searching - which is the part this app cannot and
    should not do on your behalf.
    """
    from urllib.parse import quote_plus as _q
    base = {
        "google": "https://www.google.com/search?q={q}",
        "duckduckgo": "https://duckduckgo.com/?q={q}",
        "bing": "https://www.bing.com/search?q={q}",
    }.get(engine, "https://www.google.com/search?q={q}")

    terms = find_agencies.VERTICALS.get(vertical, [vertical])[:4]
    excl = " -site:clutch.co -site:sortlist.com -site:designrush.com -site:upcity.com"
    out = []
    for term in terms:
        for template, why in SEARCH_TEMPLATES[:1]:
            q = template.format(term=term, geo=geo or "").strip() + excl
            out.append({"term": term, "why": why, "query": q,
                        "url": base.format(q=_q(q))})
    return out


# A domain with any plausible TLD, for a list you wrote yourself.
_ANY_DOMAIN_RE = re.compile(
    r"\b(?:www\.)?([a-z0-9][a-z0-9\-]{0,61}"
    r"(?:\.[a-z0-9][a-z0-9\-]{0,61})*"
    r"\.[a-z]{2,24})\b", re.I)


def looks_like_a_list(text):
    """
    Did someone paste a list they curated, or a page they copied?

    It matters. A curated list deserves the benefit of the doubt - if you wrote
    down acme.ca, you meant it - while a copied search page is mostly navigation
    and adverts and needs the strict filter. The tell is shape: a list is short
    lines that are each almost entirely one address.
    """
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines or len(lines) > 2000:
        return False
    if "<" in (text or "") and ">" in (text or ""):
        return False              # markup: a saved page, not a list
    domainish = 0
    for line in lines:
        candidate = line.split(",")[0].split("\t")[0].strip().strip('"\'')
        m = _ANY_DOMAIN_RE.fullmatch(candidate.replace("https://", "")
                                              .replace("http://", "").rstrip("/"))
        if m or common.root_domain(candidate):
            domainish += 1
    return domainish >= max(1, int(len(lines) * 0.6))


def extract_agencies(text, vertical="", geo="", permissive=None):
    """
    Pull candidate agency websites out of whatever was pasted: a list you wrote,
    a saved search page, or copied text.

    permissive=None decides for itself from the shape of the input. A curated
    list accepts any TLD; a copied page is held to the stricter filter that
    knows what a search result page is full of.

    Either way the known non-agency domains - social, directories, CDNs,
    hosting - are dropped, and results dedupe by root domain. Nothing is saved
    until you say so.
    """
    text = text or ""
    if permissive is None:
        permissive = looks_like_a_list(text)
    found, seen = [], set()

    def consider(raw):
        if permissive:
            rd = common.root_domain(raw)
            # Even a hand-written list shouldn't turn facebook.com into a prospect.
            if rd and (rd in find_agencies.NON_AGENCY_DOMAINS
                       or re.search(r"(?:cdn|static|assets|img|images|fonts)\.", rd)):
                rd = None
        else:
            rd = find_agencies._plausible_agency_domain(raw)
        if not rd or rd in seen:
            return
        seen.add(rd)
        found.append(rd)

    for url in _URL_RE.findall(text):
        # Unwrap the redirector links search results are wrapped in.
        from urllib.parse import parse_qs, unquote, urlparse as _up
        parsed = _up(unquote(url))
        qs = parse_qs(parsed.query)
        for key in ("url", "q", "uddg", "u", "target"):
            if key in qs and qs[key] and qs[key][0].startswith("http"):
                consider(qs[key][0])
                break
        else:
            consider(url)

    pattern = _ANY_DOMAIN_RE if permissive else _BARE_DOMAIN_RE
    for match in pattern.findall(text):
        consider(match)

    return [_candidate_row(d, vertical, geo) for d in found]


def _candidate_row(domain, vertical, geo):
    return {
        "agency_name": "", "agency_domain": domain, "vertical": vertical,
        "hq_location": geo, "employee_count": "", "mentions_tiktok": "",
        "tiktok_evidence": "", "client_page_url": "", "clients_found": "",
        "status": "candidate", "notes": "added from your own search",
        "last_checked": common.now_stamp(),
    }


def add_agencies(domains, vertical="", geo=""):
    """
    Save imported agencies into agencies.csv, skipping ones already known and
    any badged TikTok partner. Returns (added, skipped_known, skipped_partner).
    """
    store = Store(AGENCIES_CSV, AGENCY_FIELDS, ["agency_domain"])
    known = {r["agency_domain"] for r in store.all()}
    try:
        partners = tiktok_partners.domain_set(required=False)
    except SystemExit:
        partners = set()

    added = skipped_known = skipped_partner = 0
    for d in domains:
        rd = common.root_domain(d)
        if not rd:
            continue
        if rd in known:
            skipped_known += 1
            continue
        if rd in partners:
            skipped_partner += 1
            continue
        store.put(_candidate_row(rd, vertical, geo))
        known.add(rd)
        added += 1
    store.flush()
    return added, skipped_known, skipped_partner


def process_agencies(domains, on_event, should_stop, config=None):
    """
    Run the checking half of the pipeline over specific agencies: TikTok in
    their services, then clients, then pixel-check those clients.

    This is what runs after an import, and it is the same code the full run uses
    - imported agencies are not treated as a special case anywhere downstream.
    """
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(config or {})

    agencies = Store(AGENCIES_CSV, AGENCY_FIELDS, ["agency_domain"])
    clients = Store(CLIENTS_CSV, CLIENT_FIELDS, ["agency_domain", "client_name"])
    tags = Store(TAGS_CSV, TAG_FIELDS, ["domain"])

    wanted = {common.root_domain(d) for d in domains if common.root_domain(d)}
    todo = [a for a in agencies.all() if a["agency_domain"] in wanted] if wanted else [
        a for a in agencies.all() if not a.get("mentions_tiktok")]
    if not todo:
        on_event("info", "Nothing new to check.")
        return summary(agencies, clients, tags)

    fetcher = common.Fetcher(delay=cfg["delay"], verbose=False)
    state = load_state()

    try:
        on_event("step", f"Checking {len(todo)} agencies for TikTok in their services")
        checked = []
        for i, agency in enumerate(todo, 1):
            if should_stop():
                raise Stopped()
            try:
                row = check_agency_tiktok.check_agency(fetcher, agency, run_pixel=True)
            except Exception as e:
                row = dict(agency, status=f"crashed:{type(e).__name__}", mentions_tiktok="")
            for k in ("_pages_fetched", "_pixel_status", "_own_tiktok_pixel"):
                row.pop(k, None)
            agencies.put(row)
            checked.append(row)
            on_event("progress", f"  {row.get('agency_domain', '')}: {_tiktok_label(row)}",
                     step="tiktok", i=i, n=len(todo))

        targets = [r for r in checked if r.get("mentions_tiktok") != "yes"]
        on_event("info", f"{len(targets)} of {len(checked)} do not sell TikTok.")

        to_parse = targets or checked
        on_event("step", f"Reading client lists for {len(to_parse)} agencies")
        batch_clients = []
        for i, agency in enumerate(to_parse, 1):
            if should_stop():
                raise Stopped()
            try:
                found_clients, arow = parse_clients.parse_agency(fetcher, agency)
            except Exception as e:
                found_clients, arow = [], dict(
                    agency, status=f"crashed:{type(e).__name__}", clients_found="")
            for c in found_clients:
                c.pop("_methods", None)
                c.pop("_name_from_domain", None)
            agencies.put(arow)
            clients.put_all(found_clients)
            batch_clients += found_clients
            on_event("progress",
                     f"  {arow.get('agency_domain', '')}: "
                     f"{arow.get('clients_found') or '—'} clients ({arow.get('status', '')})",
                     step="clients", i=i, n=len(to_parse))

        seen_domains = {t["domain"] for t in tags.all()}
        to_check = []
        for c in batch_clients:
            d = (c.get("client_domain") or "").strip()
            if d and d not in seen_domains and d not in {x["domain"] for x in to_check}:
                to_check.append({"domain": d, "client_name": c.get("client_name", ""),
                                 "agency_name": c.get("agency_name", "")})
        if to_check:
            on_event("step", f"Checking {len(to_check)} client sites for a TikTok pixel")
            for i, item in enumerate(to_check, 1):
                if should_stop():
                    raise Stopped()
                session = pixel_check.requests.Session()
                try:
                    row = pixel_check.check(item["domain"], session)
                except Exception as e:
                    row = {"domain": item["domain"], "status": f"crashed:{type(e).__name__}"}
                finally:
                    session.close()
                row["client_name"] = item["client_name"]
                row["agency_name"] = item["agency_name"]
                row["last_checked"] = common.now_stamp()
                tags.put(row)
                on_event("progress", f"  {item['domain']}: {_pixel_label(row)}",
                         step="pixels", i=i, n=len(to_check))
                time.sleep(max(0.0, cfg["delay"] * 0.5))
    except Stopped:
        on_event("info", "Stopping - saving everything found so far.")
    except Exception as e:
        on_event("error", f"Unexpected error: {type(e).__name__}: {e}")
        on_event("debug", traceback.format_exc())
    finally:
        _persist(agencies, clients, tags, state, STATE_PATH, cfg, on_event)

    return summary(agencies, clients, tags)


def check_domains_only(domains, on_event, should_stop, config=None, agency_name=""):
    """
    Pixel-check a list of brand websites and stop there.

    For when the list you have is brands rather than agencies: no client parsing,
    no services pages, just "which of these are running TikTok". Results land in
    the same Ad Tags store, so the scorecard picks them up if their agency is
    known later.
    """
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(config or {})

    agencies = Store(AGENCIES_CSV, AGENCY_FIELDS, ["agency_domain"])
    clients = Store(CLIENTS_CSV, CLIENT_FIELDS, ["agency_domain", "client_name"])
    tags = Store(TAGS_CSV, TAG_FIELDS, ["domain"])
    state = load_state()

    todo, seen = [], {t["domain"] for t in tags.all()}
    for d in domains:
        rd = common.root_domain(d)
        if rd and rd not in seen:
            seen.add(rd)
            todo.append(rd)

    if not todo:
        on_event("info", "Every one of those has already been checked.")
        return summary(agencies, clients, tags)

    on_event("step", f"Checking {len(todo)} websites for a TikTok pixel")
    try:
        for i, domain in enumerate(todo, 1):
            if should_stop():
                raise Stopped()
            session = pixel_check.requests.Session()
            try:
                row = pixel_check.check(domain, session)
            except Exception as e:
                row = {"domain": domain, "status": f"crashed:{type(e).__name__}"}
            finally:
                session.close()
            if agency_name:
                row["agency_name"] = agency_name
            row["last_checked"] = common.now_stamp()
            tags.put(row)
            # Recorded as a client too, so it shows up in the scorecard rather
            # than sitting in a tab nothing joins against.
            clients.put({
                "client_name": row.get("client_name") or domain,
                "client_domain": domain, "agency_name": agency_name,
                "agency_domain": common.root_domain(agency_name) or "",
                "vertical": cfg.get("verticals", [""])[0] if cfg.get("verticals") else "",
                "source": "you provided this list", "confidence": "high",
                "last_checked": common.now_stamp(),
            })
            on_event("progress", f"  {domain}: {_pixel_label(row)}",
                     step="pixels", i=i, n=len(todo))
            time.sleep(max(0.0, cfg["delay"] * 0.5))
    except Stopped:
        on_event("info", "Stopping - saving everything checked so far.")
    except Exception as e:
        on_event("error", f"Unexpected error: {type(e).__name__}: {e}")
    finally:
        _persist(agencies, clients, tags, state, STATE_PATH, cfg, on_event)

    rows = tags.all()
    qualifying = sum(1 for t in rows if t.get("qualifies") == "yes")
    has_tt = sum(1 for t in rows if t.get("tiktok") == "yes")
    on_event("info", f"Done. {qualifying} running Meta or Google with no TikTok; "
                     f"{has_tt} already on TikTok.")
    return summary(agencies, clients, tags)
