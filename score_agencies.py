#!/usr/bin/env python3
"""
score_agencies.py - The number the whole pipeline exists to produce.

Joins Clients to Ad Tags and answers, per agency: of the clients we found and
could actually check, how many came back with no TikTok pixel?

    python score_agencies.py --clients clients.csv --tags results.csv
    python score_agencies.py --from-sheet --sheet          # read and write Sheets
    python score_agencies.py --clients clients.csv --tags results.csv --min-checked 5

An agency with fourteen clients and zero TikTok pixels across all fourteen is a
better call than one with two clients and an ambiguous read, so the scorecard is
sorted by the count of qualifying clients and every row carries its coverage -
how many of the found clients were actually reachable. A high count on thin
coverage is visible as thin, not laundered into a score.

Per-client outcomes, from the Ad Tags row:

  qualifying    reachable, no TikTok anywhere, and Meta or Google present.
                The pitch: they are buying vertical-video-adjacent media and
                have never set up TikTok.
  tiktok_free   reachable, no TikTok, but no Meta or Google either. Real, but a
                weaker call - they may not be buying paid media at all.
  has_tiktok    TikTok pixel found. Disqualifies the client, not the agency.
  unchecked     no Ad Tags row - the client has no domain, or was never run.
  unreachable   we tried and the site blocked us. NOT a clean read either way.

The scorecard lands in the Agencies tab as real columns - clients_checked,
clients_qualifying, clients_tiktok_free, clients_with_tiktok, coverage_pct - so
you can sort the sheet on the number rather than on a substring inside notes.
The same summary is also written into notes as an audit trail. The full
breakdown, including which client domains qualified, goes to
agency_scorecard.csv.
"""

import argparse
import csv
import os
import sys

import common

SCORE_FIELDS = [
    "agency_name", "agency_domain", "vertical", "mentions_tiktok",
    "clients_found", "clients_checked", "qualifying", "tiktok_free",
    "has_tiktok", "unreachable", "unchecked", "coverage_pct",
    "qualifying_client_domains", "agency_status", "notes",
]


def _read_csv(path, label):
    if not path:
        return []
    if not os.path.exists(path):
        sys.exit(f"{label} file not found: {path}")
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f)]


def classify(tag_row):
    """One Ad Tags row -> one outcome. Unknown shapes are unchecked, never clean."""
    if not tag_row:
        return "unchecked"
    status = (tag_row.get("status") or "").strip().lower()
    if status != "ok":
        # bad_input, unreachable, fetch_failed:*, crashed:* - all the same thing
        # for our purposes: we did not get a look at this site.
        return "unreachable" if status else "unchecked"
    if (tag_row.get("tiktok") or "").strip().lower() == "yes":
        return "has_tiktok"
    if (tag_row.get("qualifies") or "").strip().lower() == "yes":
        return "qualifying"
    return "tiktok_free"


def score(clients, tags, agencies=None, min_confidence=None):
    """Fold clients and their pixel results into one row per agency."""
    rank = {"high": 0, "medium": 1, "low": 2}
    floor = rank.get(min_confidence, 2)

    by_domain = {}
    for t in tags:
        rd = common.root_domain(t.get("domain", ""))
        if rd:
            by_domain[rd] = t

    agency_meta = {}
    for a in (agencies or []):
        rd = common.root_domain(a.get("agency_domain", ""))
        if rd:
            agency_meta[rd] = a

    buckets = {}
    for c in clients:
        conf = (c.get("confidence") or "low").strip().lower()
        if rank.get(conf, 2) > floor:
            continue
        ard = common.root_domain(c.get("agency_domain", ""))
        if not ard:
            continue
        b = buckets.setdefault(ard, {
            "agency_name": c.get("agency_name", ""),
            "agency_domain": ard,
            "vertical": c.get("vertical", ""),
            "counts": {"qualifying": 0, "tiktok_free": 0, "has_tiktok": 0,
                       "unreachable": 0, "unchecked": 0},
            "clients_found": 0,
            "qualifying_domains": [],
        })
        b["clients_found"] += 1
        if not b["agency_name"] and c.get("agency_name"):
            b["agency_name"] = c["agency_name"]

        crd = common.root_domain(c.get("client_domain", ""))
        outcome = classify(by_domain.get(crd)) if crd else "unchecked"
        b["counts"][outcome] += 1
        if outcome == "qualifying" and crd:
            b["qualifying_domains"].append(crd)

    # An agency with no client rows at all still belongs on the scorecard - a
    # zero that came from needs_manual_review is a prospect, not a dead end.
    for rd, a in agency_meta.items():
        if rd not in buckets:
            buckets[rd] = {
                "agency_name": a.get("agency_name", ""), "agency_domain": rd,
                "vertical": a.get("vertical", ""),
                "counts": {k: 0 for k in ("qualifying", "tiktok_free", "has_tiktok",
                                          "unreachable", "unchecked")},
                "clients_found": 0, "qualifying_domains": [],
            }

    rows = []
    for rd, b in buckets.items():
        c = b["counts"]
        checked = c["qualifying"] + c["tiktok_free"] + c["has_tiktok"]
        meta = agency_meta.get(rd, {})
        rows.append({
            "agency_name": b["agency_name"] or meta.get("agency_name", ""),
            "agency_domain": rd,
            "vertical": b["vertical"] or meta.get("vertical", ""),
            "mentions_tiktok": meta.get("mentions_tiktok", ""),
            "clients_found": b["clients_found"],
            "clients_checked": checked,
            "qualifying": c["qualifying"],
            "tiktok_free": c["qualifying"] + c["tiktok_free"],
            "has_tiktok": c["has_tiktok"],
            "unreachable": c["unreachable"],
            "unchecked": c["unchecked"],
            "coverage_pct": (round(100 * checked / b["clients_found"])
                             if b["clients_found"] else 0),
            "qualifying_client_domains": " ".join(sorted(b["qualifying_domains"])[:20]),
            "agency_status": meta.get("status", ""),
            "notes": meta.get("notes", ""),
        })

    # Most qualifying clients first; ties broken by coverage, so a clean read on
    # eight beats an ambiguous read on eight.
    rows.sort(key=lambda r: (-r["qualifying"], -r["tiktok_free"], -r["coverage_pct"],
                             -r["clients_found"]))
    return rows


def to_agency_rows(scored):
    """
    Fold the scorecard back into the Agencies schema: the count columns get the
    numbers, and notes gets a SCORE summary line, prefixed so it can be found
    with a filter and replaced rather than stacked on the next run.
    """
    out = []
    for r in scored:
        prior = (r.get("notes") or "")
        prior = " | ".join(p for p in prior.split(" | ") if not p.startswith("SCORE "))
        summary = (f"SCORE qualifying={r['qualifying']} tiktok_free={r['tiktok_free']} "
                   f"has_tiktok={r['has_tiktok']} checked={r['clients_checked']}/"
                   f"{r['clients_found']} coverage={r['coverage_pct']}% "
                   f"unreachable={r['unreachable']} unchecked={r['unchecked']}")
        out.append({
            "agency_name": r["agency_name"],
            "agency_domain": r["agency_domain"],
            "vertical": r["vertical"],
            "mentions_tiktok": r["mentions_tiktok"],
            "clients_found": str(r["clients_found"]),
            "status": r["agency_status"] or "scored",
            "notes": " | ".join(p for p in (prior, summary) if p),
            # Real columns, so the sheet sorts on a number instead of on a
            # substring buried in notes. The notes summary stays for the audit
            # trail and because it survives a column being hidden.
            "clients_checked": str(r["clients_checked"]),
            "clients_qualifying": str(r["qualifying"]),
            "clients_tiktok_free": str(r["tiktok_free"]),
            "clients_with_tiktok": str(r["has_tiktok"]),
            "coverage_pct": str(r["coverage_pct"]),
        })
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Rank agencies by how many of their clients came back TikTok-free.")
    ap.add_argument("--clients", help="clients.csv from parse_clients.py")
    ap.add_argument("--tags", help="results.csv from pixel_check.py")
    ap.add_argument("--agencies", help="agencies CSV, for names/verticals/status")
    ap.add_argument("--from-sheet", action="store_true",
                    help="read Clients, Ad Tags and Agencies from the workbook instead")
    ap.add_argument("--min-confidence", choices=["high", "medium", "low"], default="low",
                    help="only count clients at or above this confidence (default low)")
    ap.add_argument("--min-checked", type=int, default=0,
                    help="hide agencies with fewer than N clients actually checked")
    ap.add_argument("-o", "--output", default="agency_scorecard.csv")
    ap.add_argument("--top", type=int, default=25, help="rows to print (default 25)")
    common.add_sheet_args(ap)
    args = ap.parse_args()

    if args.from_sheet:
        try:
            import sheets
        except SystemExit as e:
            sys.exit(str(e))
        book = sheets.connect(args.sheet_id)
        clients = sheets.read_tab(book, "Clients")
        tags = sheets.read_tab(book, "Ad Tags")
        agencies = sheets.read_tab(book, "Agencies")
        print(f"Read from {book.url}: {len(clients)} clients, {len(tags)} tag rows, "
              f"{len(agencies)} agencies", file=sys.stderr)
    else:
        if not args.clients or not args.tags:
            ap.error("--clients and --tags are required unless --from-sheet is given")
        clients = _read_csv(args.clients, "Clients")
        tags = _read_csv(args.tags, "Ad Tags")
        agencies = _read_csv(args.agencies, "Agencies") if args.agencies else []

    scored = score(clients, tags, agencies, args.min_confidence)
    shown = [r for r in scored if r["clients_checked"] >= args.min_checked]

    if not scored:
        sys.exit("Nothing to score. Check that the clients file has agency_domain "
                 "values and the tags file has domain values.")

    width = 34
    print(f"\n{'agency':{width}} {'vert':<14} {'qual':>4} {'free':>4} {'tt':>3} "
          f"{'chk':>4} {'found':>5} {'cov':>4}  mentions", file=sys.stderr)
    print("-" * (width + 48), file=sys.stderr)
    for r in shown[:args.top]:
        label = (r["agency_name"] or r["agency_domain"])[:width - 1]
        print(f"{label:{width}} {r['vertical'][:13]:<14} {r['qualifying']:>4} "
              f"{r['tiktok_free']:>4} {r['has_tiktok']:>3} {r['clients_checked']:>4} "
              f"{r['clients_found']:>5} {str(r['coverage_pct']) + '%':>4}  "
              f"{r['mentions_tiktok'] or '-'}", file=sys.stderr)

    hidden = len(scored) - len(shown)
    if hidden:
        print(f"\n  {hidden} agency/agencies hidden by --min-checked {args.min_checked}",
              file=sys.stderr)
    totals = {k: sum(r[k] for r in scored)
              for k in ("qualifying", "tiktok_free", "has_tiktok", "unreachable",
                        "unchecked", "clients_found")}
    print(f"\n  {len(scored)} agencies, {totals['clients_found']} clients", file=sys.stderr)
    print(f"    qualifying:  {totals['qualifying']}", file=sys.stderr)
    print(f"    tiktok-free: {totals['tiktok_free']}", file=sys.stderr)
    print(f"    has tiktok:  {totals['has_tiktok']}", file=sys.stderr)
    print(f"    unreachable: {totals['unreachable']}  (blocked, not clean)", file=sys.stderr)
    print(f"    unchecked:   {totals['unchecked']}  (no domain, or never run "
          f"through pixel_check)", file=sys.stderr)

    if args.dry_run:
        print("\n--dry-run: nothing written.", file=sys.stderr)
        return

    common.write_csv(args.output, SCORE_FIELDS, scored)
    if args.sheet:
        common.push_sheet("Agencies", to_agency_rows(scored), args.sheet_id, args.output)


if __name__ == "__main__":
    main()
