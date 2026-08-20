#!/usr/bin/env python3
"""
feedback.py - The loop that makes the parser get better instead of staying wrong.

The extractors in parse_clients.py are heuristics. Heuristics are wrong in
specific, repeatable ways, and the only thing that reliably fixes them is you
looking at the output and saying which rows are junk. This module makes that
judgment stick, so you never correct the same mistake twice.

How the loop runs:

  1. parse_clients.py writes to the Clients tab.
  2. You skim it and put verdicts in the Feedback tab - one line per row you
     disagree with. You do not have to review everything; only the wrong ones
     carry information you can't get any other way.
  3. python feedback.py --learn
     reads those verdicts, turns them into rules in data/learned_rules.json,
     and recomputes how much each extraction method is actually worth.
  4. The next parse_clients.py run loads those rules and applies them. A label
     you called junk is never recorded again. A name you corrected is corrected
     everywhere. A method you keep marking wrong loses its confidence rating.

Verdicts, in the Feedback tab's `verdict` column:

  bad           not a client. Becomes a suppression rule.
  good          confirmed. Counts toward that method's precision.
  rename        wrong name, right client. Put the right name in correct_value.
  wrong_domain  right client, wrong domain. Put the right domain in correct_value.
  missed        a client we never found. correct_value holds the name; recorded
                as a recall miss against that agency so you can see which sites
                the static parse cannot read.

Nothing here guesses. Every rule traces to a row you wrote.

    python feedback.py --learn                  # verdicts -> rules
    python feedback.py --learn --from-sheet     # read verdicts from the workbook
    python feedback.py --report                 # what's broken, as markdown
    python feedback.py --show-rules             # what it has learned so far
    python feedback.py --template -o review.csv # a Feedback CSV to fill in
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone

import common

RULES_PATH = os.path.join("data", "learned_rules.json")
HEALTH_PATH = os.path.join("data", "source_health.json")

VERDICTS = ("good", "bad", "rename", "wrong_domain", "missed")
ENTITY_TYPES = ("client", "agency_tiktok", "agency")

FEEDBACK_FIELDS = ["entity_type", "entity_key", "agency_domain", "verdict",
                   "correct_value", "method", "note", "reviewed_at", "applied"]

# A method needs this many labelled examples before its confidence is retuned.
# Below it, one bad afternoon of review would swing the whole pipeline.
MIN_SAMPLES_TO_RETUNE = 20
PRECISION_HIGH = 0.90
PRECISION_MEDIUM = 0.70

EMPTY_RULES = {
    "version": 1,
    "updated_at": "",
    "junk_labels": [],          # names that are never a client, anywhere
    "junk_domains": [],         # domains that are never a client, anywhere
    "agency_blocklist": {},     # agency_domain -> [labels wrong for that agency only]
    "name_corrections": {},     # name_key -> the name you actually want
    "domain_corrections": {},   # name_key -> the domain you actually want
    "method_stats": {},         # method -> {good, bad, precision}
    "confidence_overrides": {},  # method -> high|medium|low, learned from precision
    "recall_misses": {},        # agency_domain -> [names we never found]
    "counts": {"verdicts_seen": 0},
}


# --------------------------------------------------------------------------
# Rules: load, save, apply
# --------------------------------------------------------------------------

def load_rules(path=RULES_PATH):
    """Read the learned rules. A missing file is normal - it means nothing learned yet."""
    if not os.path.exists(path):
        return json.loads(json.dumps(EMPTY_RULES))
    try:
        with open(path, encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit(
            f"Learned rules at {path} are unreadable: {e}\n"
            f"Delete the file to start over - the verdicts in the Feedback tab "
            f"are the source of truth and can be replayed with --learn.")
    merged = json.loads(json.dumps(EMPTY_RULES))
    merged.update(blob)
    return merged


def save_rules(rules, path=RULES_PATH):
    rules["updated_at"] = datetime.now(timezone.utc).isoformat()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rules, f, indent=2, sort_keys=True)


class Rules:
    """
    The learned rules, in the shape parse_clients.py wants them.

    Deliberately dumb: exact-match lookups on a normalised key. A fuzzy rule
    that suppressed things you didn't ask it to suppress would be worse than no
    rule at all, because you would stop trusting the output.
    """

    def __init__(self, blob=None, path=RULES_PATH):
        self.blob = blob if blob is not None else load_rules(path)
        self._junk = _match_set(self.blob.get("junk_labels", []))
        self._junk_domains = {(d or "").strip().lower()
                              for d in self.blob.get("junk_domains", [])}
        self._per_agency = {
            (a or "").strip().lower(): _match_set(labels)
            for a, labels in self.blob.get("agency_blocklist", {}).items()
        }
        self._names = {_key(k): v for k, v in self.blob.get("name_corrections", {}).items()}
        self._domains = {_key(k): v for k, v in self.blob.get("domain_corrections", {}).items()}
        self._confidence = dict(self.blob.get("confidence_overrides", {}))

    @property
    def empty(self):
        return not (self._junk or self._junk_domains or self._per_agency
                    or self._names or self._domains or self._confidence)

    def is_junk(self, name, domain="", agency_domain=""):
        forms = _match_forms(name)
        # Also test the domain's own stem: a label you rejected can come back
        # under a name the parser derived from the domain instead. "Northbeam
        # Media" blocked has to also stop "Northbeammedia" from northbeammedia.com.
        if domain:
            d = domain.strip().lower()
            if d in self._junk_domains:
                return True
            forms |= _match_forms(d.split(".")[0])
        if forms & self._junk:
            return True
        per = self._per_agency.get((agency_domain or "").strip().lower())
        return bool(per and (forms & per))

    def correct_name(self, name):
        return self._names.get(_key(name), name)

    def correct_domain(self, name, current=""):
        return self._domains.get(_key(name), current)

    def confidence_for(self, method, default):
        return self._confidence.get(method, default)

    def summary(self):
        # Counted off the source lists: each label expands to several match
        # forms internally, which would otherwise inflate the number reported.
        return (f"{len(self.blob.get('junk_labels', []))} junk labels, "
                f"{len(self._junk_domains)} junk domains, "
                f"{sum(len(v) for v in self.blob.get('agency_blocklist', {}).values())} "
                f"per-agency rules, "
                f"{len(self._names)} name corrections, "
                f"{len(self._domains)} domain corrections, "
                f"{len(self._confidence)} confidence overrides")


def _match_forms(s):
    """
    Every form a name can legitimately take, for exact-match rule lookup.

    Both the token key ("northbeam media") and the squashed form
    ("northbeammedia"), because the same client arrives written out from alt
    text and run together from a domain, and a rule should catch both.
    """
    k = _key(s)
    if not k:
        return set()
    return {k, k.replace(" ", "")}


def _match_set(items):
    out = set()
    for item in items:
        out |= _match_forms(item)
    return out


def _key(s):
    """Normalise a name for matching. Mirrors parse_clients.name_key."""
    toks = [t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) > 1]
    drop = {"the", "inc", "llc", "ltd", "co", "corp", "company", "group", "and"}
    core = [t for t in toks if t not in drop] or toks
    return " ".join(core)


# --------------------------------------------------------------------------
# Learning
# --------------------------------------------------------------------------

def read_feedback(path=None, from_sheet=False, sheet_id=None):
    """Verdict rows, from a CSV or from the Feedback tab."""
    if from_sheet:
        try:
            import sheets
        except SystemExit as e:
            sys.exit(str(e))
        book = sheets.connect(sheet_id)
        rows = sheets.read_tab(book, "Feedback")
        print(f"Read {len(rows)} verdict(s) from {book.url}", file=sys.stderr)
        return rows
    if not path:
        sys.exit("Give --feedback <csv> or --from-sheet.")
    if not os.path.exists(path):
        sys.exit(f"Feedback file not found: {path}\n"
                 f"Create one with: python feedback.py --template -o {path}")
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def learn(feedback_rows, rules=None):
    """
    Fold verdicts into rules. Returns (rules, report).

    Replays cleanly: running this twice over the same verdicts produces the same
    rules, so the Feedback tab stays the source of truth and the JSON is only a
    cache of it.
    """
    rules = rules if rules is not None else load_rules()
    report = {"applied": 0, "skipped": [], "by_verdict": {}}

    junk = set(rules.get("junk_labels", []))
    junk_domains = set(rules.get("junk_domains", []))
    per_agency = {k: set(v) for k, v in rules.get("agency_blocklist", {}).items()}
    names = dict(rules.get("name_corrections", {}))
    domains = dict(rules.get("domain_corrections", {}))
    misses = {k: set(v) for k, v in rules.get("recall_misses", {}).items()}
    stats = {m: dict(v) for m, v in rules.get("method_stats", {}).items()}

    for i, row in enumerate(feedback_rows, start=2):
        verdict = (row.get("verdict") or "").strip().lower()
        entity = (row.get("entity_key") or "").strip()
        etype = (row.get("entity_type") or "client").strip().lower() or "client"
        agency = common.root_domain(row.get("agency_domain", "")) or ""
        correct = (row.get("correct_value") or "").strip()
        method = (row.get("method") or "").strip().lower()

        if not verdict:
            continue
        if verdict not in VERDICTS:
            report["skipped"].append(f"row {i}: unknown verdict {verdict!r} "
                                     f"(expected one of {', '.join(VERDICTS)})")
            continue
        if not entity and verdict != "missed":
            report["skipped"].append(f"row {i}: verdict {verdict!r} with no entity_key")
            continue
        if verdict in ("rename", "wrong_domain", "missed") and not correct:
            report["skipped"].append(
                f"row {i}: verdict {verdict!r} needs a correct_value")
            continue

        report["by_verdict"][verdict] = report["by_verdict"].get(verdict, 0) + 1
        report["applied"] += 1

        if etype != "client":
            # agency-level verdicts count toward stats but drive no parse rules;
            # they surface in the report so you can see what you disagreed with.
            _bump(stats, method or etype, "good" if verdict == "good" else "bad")
            continue

        if verdict == "bad":
            _bump(stats, method, "bad")
            looks_like_domain = bool(common.root_domain(entity)) and " " not in entity
            if looks_like_domain:
                junk_domains.add(common.root_domain(entity))
            elif agency:
                # Scoped to the agency by default: "Summit" may be furniture on
                # one site and a real client on another. Widen it by hand in the
                # JSON if you decide it is junk everywhere.
                per_agency.setdefault(agency, set()).add(entity)
            else:
                junk.add(entity)
        elif verdict == "good":
            _bump(stats, method, "good")
        elif verdict == "rename":
            _bump(stats, method, "bad")
            names[entity] = correct
        elif verdict == "wrong_domain":
            _bump(stats, method, "bad")
            fixed = common.root_domain(correct)
            if not fixed:
                report["skipped"].append(
                    f"row {i}: correct_value {correct!r} is not a domain")
                report["applied"] -= 1
                continue
            domains[entity] = fixed
        elif verdict == "missed":
            misses.setdefault(agency or "(unknown agency)", set()).add(correct)

    rules["junk_labels"] = sorted(junk)
    rules["junk_domains"] = sorted(junk_domains)
    rules["agency_blocklist"] = {k: sorted(v) for k, v in sorted(per_agency.items()) if v}
    rules["name_corrections"] = dict(sorted(names.items()))
    rules["domain_corrections"] = dict(sorted(domains.items()))
    rules["recall_misses"] = {k: sorted(v) for k, v in sorted(misses.items()) if v}
    rules["method_stats"] = _finalise_stats(stats)
    rules["confidence_overrides"] = _retune(rules["method_stats"], report)
    rules["counts"]["verdicts_seen"] = report["applied"]
    return rules, report


def _bump(stats, method, outcome):
    if not method:
        method = "(unrecorded)"
    s = stats.setdefault(method, {"good": 0, "bad": 0})
    s[outcome] = s.get(outcome, 0) + 1


def _finalise_stats(stats):
    out = {}
    for method, s in sorted(stats.items()):
        good, bad = s.get("good", 0), s.get("bad", 0)
        total = good + bad
        out[method] = {"good": good, "bad": bad, "samples": total,
                       "precision": round(good / total, 3) if total else None}
    return out


def _retune(method_stats, report):
    """
    Set each method's confidence from its measured precision.

    This is the part that improves on its own, and it is deliberately
    conservative: a method needs MIN_SAMPLES_TO_RETUNE labelled examples before
    its rating moves at all, and the thresholds are wide. The goal is to stop
    you re-reviewing a method that has proven itself, not to chase noise.
    """
    overrides = {}
    for method, s in method_stats.items():
        if method in ("(unrecorded)", "agency_tiktok", "agency"):
            continue
        if not s["samples"] or s["samples"] < MIN_SAMPLES_TO_RETUNE:
            continue
        p = s["precision"]
        level = "high" if p >= PRECISION_HIGH else (
            "medium" if p >= PRECISION_MEDIUM else "low")
        overrides[method] = level
        report.setdefault("retuned", []).append(
            f"{method}: precision {p:.0%} over {s['samples']} reviewed -> {level}")
    return overrides


# --------------------------------------------------------------------------
# Source health - is a directory or extractor quietly dead?
# --------------------------------------------------------------------------

def record_health(source_counts, path=HEALTH_PATH, keep=20):
    """
    Append one run's per-source outcome. find_agencies.py calls this so a
    directory that starts returning nothing shows up as a trend rather than as
    a run you happened not to look at.
    """
    history = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                history = json.load(f)
        except (OSError, ValueError):
            history = {}
    stamp = datetime.now(timezone.utc).isoformat()
    for source, outcome in source_counts.items():
        runs = history.setdefault(source, [])
        runs.append({"at": stamp, **outcome})
        del runs[:-keep]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, sort_keys=True)


def health_report(path=HEALTH_PATH, window=5):
    """Sources whose recent runs look broken. Returns a list of finding strings."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            history = json.load(f)
    except (OSError, ValueError):
        return [f"source_health.json at {path} is unreadable"]
    findings = []
    for source, runs in sorted(history.items()):
        recent = runs[-window:]
        if not recent:
            continue
        found = sum(r.get("found", 0) for r in recent)
        blocked = sum(1 for r in recent if r.get("blocked"))
        if found == 0:
            findings.append(
                f"**{source}** returned 0 agencies across its last {len(recent)} run(s). "
                + ("Looks like a bot challenge, not an empty result - save the "
                   "listing page from a browser and use --from-html."
                   if blocked else
                   "Not blocked, so this is most likely a changed page layout: "
                   "the profile-link or website selector in DIRECTORIES needs updating."))
        elif blocked >= max(2, len(recent) // 2):
            findings.append(
                f"**{source}** was challenged on {blocked} of its last "
                f"{len(recent)} run(s). Coverage from this source is unreliable.")
    return findings


# --------------------------------------------------------------------------
# The report - written for a person, and for pasting into a Claude session
# --------------------------------------------------------------------------

def build_report(rules, clients=None, agencies=None, scorecard=None):
    clients, agencies = clients or [], agencies or []
    L = []
    L.append("# TikTok Gap - pipeline health report")
    L.append("")
    L.append(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    L.append("")

    stats = rules.get("method_stats", {})
    reviewed = sum(s.get("samples", 0) for s in stats.values())
    L.append("## What the parser has learned")
    L.append("")
    L.append(f"- {reviewed} row(s) reviewed so far")
    L.append(f"- Rules in force: {Rules(rules).summary()}")
    if rules.get("updated_at"):
        L.append(f"- Rules last updated: {rules['updated_at'][:19]}")
    L.append("")

    if stats:
        L.append("### Extraction method precision")
        L.append("")
        L.append("| method | confirmed | wrong | reviewed | precision | confidence |")
        L.append("|---|---:|---:|---:|---:|---|")
        overrides = rules.get("confidence_overrides", {})
        default = {"alt_text": "high", "outbound_link": "high",
                   "image_filename": "medium", "case_study_title": "low"}
        for method, s in sorted(stats.items(),
                                key=lambda kv: -(kv[1].get("samples") or 0)):
            p = s.get("precision")
            conf = overrides.get(method, default.get(method, "-"))
            tuned = " *(learned)*" if method in overrides else ""
            L.append(f"| `{method}` | {s.get('good', 0)} | {s.get('bad', 0)} | "
                     f"{s.get('samples', 0)} | "
                     f"{f'{p:.0%}' if p is not None else '-'} | {conf}{tuned} |")
        L.append("")
        thin = [m for m, s in stats.items()
                if 0 < s.get("samples", 0) < MIN_SAMPLES_TO_RETUNE]
        if thin:
            L.append(f"Methods with too few reviews to retune "
                     f"(needs {MIN_SAMPLES_TO_RETUNE}): {', '.join(sorted(thin))}.")
            L.append("")

    findings = health_report()
    L.append("## Sources that look broken")
    L.append("")
    if findings:
        for f in findings:
            L.append(f"- {f}")
    else:
        L.append("Nothing flagged. (No history yet if you haven't run "
                 "`find_agencies.py`.)")
    L.append("")

    review = [a for a in agencies if a.get("status") == "needs_manual_review"]
    unreachable = [a for a in agencies if a.get("status") == "unreachable"]
    L.append("## Agencies the static parse could not read")
    L.append("")
    L.append(f"- {len(review)} marked `needs_manual_review` (JS-rendered client page)")
    L.append(f"- {len(unreachable)} marked `unreachable`")
    if review:
        L.append("")
        L.append("These are prospects, not dead ends - the page exists, we just "
                 "can't read it without a browser:")
        L.append("")
        for a in review[:20]:
            L.append(f"- `{a.get('agency_domain', '')}` - "
                     f"{a.get('client_page_url') or 'no client page identified'}")
        if len(review) > 20:
            L.append(f"- ... and {len(review) - 20} more")
    L.append("")

    misses = rules.get("recall_misses", {})
    if misses:
        L.append("## Clients you told us we missed")
        L.append("")
        L.append("Each of these is a recall failure worth a look - if several "
                 "share a pattern, that's a new extractor.")
        L.append("")
        for agency, names in list(misses.items())[:15]:
            L.append(f"- `{agency}`: {', '.join(names[:8])}"
                     + (f" (+{len(names) - 8} more)" if len(names) > 8 else ""))
        L.append("")

    if clients:
        no_domain = [c for c in clients if not (c.get("client_domain") or "").strip()]
        low = [c for c in clients if (c.get("confidence") or "") == "low"]
        L.append("## Client rows needing attention")
        L.append("")
        L.append(f"- {len(no_domain)}/{len(clients)} have no domain, so they can "
                 f"never be pixel-checked")
        L.append(f"- {len(low)} {'row is' if len(low) == 1 else 'rows are'} low "
                 f"confidence and should be reviewed before use")
        L.append("")

    if scorecard:
        L.append("## Top agencies right now")
        L.append("")
        L.append("| agency | qualifying | tiktok-free | checked | found | coverage |")
        L.append("|---|---:|---:|---:|---:|---:|")
        for r in scorecard[:10]:
            L.append(f"| {r.get('agency_name') or r.get('agency_domain')} | "
                     f"{r.get('qualifying', '')} | {r.get('tiktok_free', '')} | "
                     f"{r.get('clients_checked', '')} | {r.get('clients_found', '')} | "
                     f"{r.get('coverage_pct', '')}% |")
        L.append("")

    L.append("## What to do next")
    L.append("")
    L.append("1. Review the Clients tab and add verdicts to the Feedback tab "
             "(`good` / `bad` / `rename` / `wrong_domain` / `missed`).")
    L.append("2. Run `python feedback.py --learn --from-sheet` to fold them in.")
    L.append("3. Re-run `parse_clients.py`; the corrections apply automatically.")
    if findings:
        L.append("4. Fix the broken source(s) above - the selectors live in "
                 "`DIRECTORIES` in `find_agencies.py`.")
    L.append("")
    return "\n".join(L)


def template_rows(clients, limit=0):
    """A Feedback CSV pre-filled with the rows most worth your attention."""
    rank = {"low": 0, "medium": 1, "high": 2}
    ordered = sorted(clients, key=lambda c: (rank.get(c.get("confidence", "low"), 0),
                                             c.get("agency_domain", "")))
    if limit:
        ordered = ordered[:limit]
    out = []
    for c in ordered:
        method = (c.get("source", "").split(" | ")[0] or "").split("+")[0]
        out.append({
            "entity_type": "client",
            "entity_key": c.get("client_name", ""),
            "agency_domain": c.get("agency_domain", ""),
            "verdict": "",          # you fill this in
            "correct_value": "",
            "method": method,
            "note": f"confidence={c.get('confidence', '')} "
                    f"domain={c.get('client_domain', '') or '(none)'}",
            "reviewed_at": "",
            "applied": "",
        })
    return out


def _read_csv(path, label):
    if not path:
        return []
    if not os.path.exists(path):
        sys.exit(f"{label} file not found: {path}")
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main():
    ap = argparse.ArgumentParser(
        description="Turn your review verdicts into rules the parser applies.")
    ap.add_argument("--learn", action="store_true",
                    help="fold verdicts into data/learned_rules.json")
    ap.add_argument("--report", action="store_true",
                    help="write a markdown health report")
    ap.add_argument("--show-rules", action="store_true",
                    help="print what has been learned so far")
    ap.add_argument("--template", action="store_true",
                    help="write a Feedback CSV pre-filled from clients.csv")
    ap.add_argument("--reset", action="store_true",
                    help="discard learned rules and relearn from verdicts only")
    ap.add_argument("--feedback", help="a Feedback CSV of verdicts")
    ap.add_argument("--from-sheet", action="store_true",
                    help="read verdicts (and data for the report) from the workbook")
    ap.add_argument("--clients", help="clients.csv, for --template and --report")
    ap.add_argument("--agencies", help="agencies CSV, for --report")
    ap.add_argument("--scorecard", help="agency_scorecard.csv, for --report")
    ap.add_argument("--rules-path", default=RULES_PATH)
    ap.add_argument("-o", "--output", help="where to write --report or --template")
    ap.add_argument("--limit", type=int, default=0, help="cap rows in --template")
    ap.add_argument("--sheet", action="store_true",
                    help="with --template, write the rows to the Feedback tab")
    ap.add_argument("--sheet-id", default=None)
    args = ap.parse_args()

    if not any((args.learn, args.report, args.show_rules, args.template)):
        ap.error("pick one of --learn, --report, --show-rules, --template")

    if args.show_rules:
        rules = load_rules(args.rules_path)
        print(json.dumps(rules, indent=2, sort_keys=True))
        return

    if args.template:
        clients = (_read_csv(args.clients, "Clients") if args.clients
                   else _sheet_tab("Clients", args.sheet_id) if args.from_sheet else [])
        if not clients:
            sys.exit("--template needs --clients clients.csv or --from-sheet.")
        rows = template_rows(clients, args.limit)
        out = args.output or "review.csv"
        common.write_csv(out, FEEDBACK_FIELDS, rows)
        print(f"\nFill in the `verdict` column, then:\n"
              f"    python feedback.py --learn --feedback {out}", file=sys.stderr)
        if args.sheet:
            common.push_sheet("Feedback", rows, args.sheet_id, out)
        return

    if args.learn:
        rows = read_feedback(args.feedback, args.from_sheet, args.sheet_id)
        if not rows:
            sys.exit("No verdicts found. Nothing to learn from.")
        base = None if args.reset else load_rules(args.rules_path)
        if args.reset:
            print("--reset: discarding learned rules and relearning from verdicts.",
                  file=sys.stderr)
            base = json.loads(json.dumps(EMPTY_RULES))
        rules, report = learn(rows, base)
        save_rules(rules, args.rules_path)

        print(f"\nLearned from {report['applied']} verdict(s) "
              f"of {len(rows)} row(s) read.", file=sys.stderr)
        for verdict, n in sorted(report["by_verdict"].items()):
            print(f"    {verdict:14} {n}", file=sys.stderr)
        for line in report.get("retuned", []):
            print(f"  retuned: {line}", file=sys.stderr)
        if report["skipped"]:
            print(f"\n  {len(report['skipped'])} row(s) skipped:", file=sys.stderr)
            for s in report["skipped"][:12]:
                print(f"    {s}", file=sys.stderr)
        print(f"\nRules now: {Rules(rules).summary()}", file=sys.stderr)
        print(f"Written to {args.rules_path}. They apply on the next "
              f"parse_clients.py run.", file=sys.stderr)
        if not args.report:
            return

    if args.report:
        rules = load_rules(args.rules_path)
        clients = (_read_csv(args.clients, "Clients") if args.clients
                   else _sheet_tab("Clients", args.sheet_id) if args.from_sheet else [])
        agencies = (_read_csv(args.agencies, "Agencies") if args.agencies
                    else _sheet_tab("Agencies", args.sheet_id) if args.from_sheet else [])
        scorecard = _read_csv(args.scorecard, "Scorecard") if args.scorecard else []
        text = build_report(rules, clients, agencies, scorecard)
        out = args.output or "feedback_report.md"
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print(text)
        print(f"\nWrote {out}", file=sys.stderr)


def _sheet_tab(tab, sheet_id):
    try:
        import sheets
    except SystemExit as e:
        sys.exit(str(e))
    return sheets.read_tab(sheets.connect(sheet_id), tab)


if __name__ == "__main__":
    main()
