#!/usr/bin/env python3
"""
test_pipeline.py - The long-running job must survive being interrupted.

Run:  python tests/test_pipeline.py

The app is meant to be left running for hours and stopped whenever. That makes
three properties load-bearing, and each gets a test:

  * a batch's results are on disk before the next one starts
  * stopping is prompt and loses nothing
  * restarting resumes instead of re-crawling what's already done
"""

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pipeline  # noqa: E402

PASS, FAIL = [], []


def check(label, condition, detail=""):
    (PASS if condition else FAIL).append(label)
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"\n        {detail}" if detail and not condition else ""))


def test_store_upserts_and_merges():
    print("\nthe CSV store")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "s.csv")
        s = pipeline.Store(path, ["a", "b", "c"], ["a"])
        s.put({"a": "x.com", "b": "one", "c": ""})
        s.put({"a": "x.com", "b": "", "c": "three"})
        s.put({"a": "y.com", "b": "two", "c": ""})
        check("one row per key", len(s) == 2, f"len={len(s)}")
        row = [r for r in s.all() if r["a"] == "x.com"][0]
        check("a later pass fills blanks without wiping what we knew",
              row["b"] == "one" and row["c"] == "three", str(row))

        s.flush()
        reloaded = pipeline.Store(path, ["a", "b", "c"], ["a"])
        check("survives a reload", len(reloaded) == 2, f"len={len(reloaded)}")
        check("values survive a reload",
              [r for r in reloaded.all() if r["a"] == "x.com"][0]["c"] == "three")

        s.put({"a": "", "b": "no key"})
        check("a row with no key is refused", len(s) == 2, f"len={len(s)}")


def test_state_is_atomic_and_recovers():
    print("\nrun state")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.json")
        check("missing state reads as a fresh run",
              pipeline.load_state(path)["done_batches"] == [])

        pipeline.save_state({"done_batches": [["home_services", "Phoenix AZ"]]}, path)
        check("saved state reads back",
              pipeline.load_state(path)["done_batches"] == [["home_services", "Phoenix AZ"]])
        check("no temp file left behind", not os.path.exists(path + ".tmp"))

        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        recovered = pipeline.load_state(path)
        check("a corrupted state file resets rather than crashing",
              recovered["done_batches"] == [] and "note" in recovered, str(recovered))

        pipeline.reset_state(path)
        check("reset removes it", not os.path.exists(path))


def test_batches_cover_every_pair():
    print("\nwork planning")
    b = pipeline.batches({"verticals": ["home_services", "dtc"],
                          "metros": ["Phoenix AZ", "Denver CO"]})
    check("every vertical x metro pair", len(b) == 4, str(b))
    check("pairs are (vertical, metro)", b[0] == ("home_services", "Phoenix AZ"), str(b[0]))
    default = pipeline.batches({"verticals": ["dtc"], "metros": []})
    check("no metros given falls back to the built-in list",
          len(default) > 10, f"got {len(default)}")


def test_stop_is_prompt_and_saves():
    print("\nstopping")
    tmp = tempfile.mkdtemp()
    cwd = os.getcwd()
    try:
        for f in ("common.py", "sheets.py", "pixel_check.py", "find_agencies.py",
                  "check_agency_tiktok.py", "parse_clients.py", "score_agencies.py",
                  "feedback.py", "tiktok_partners.py", "pipeline.py"):
            shutil.copy(os.path.join(ROOT, f), os.path.join(tmp, f))
        os.chdir(tmp)
        sys.path.insert(0, tmp)

        import find_agencies as fa
        calls = {"n": 0}

        def no_network(fetcher, engine, term, geo, vertical, max_results, failures):
            calls["n"] += 1
            failures.append({"source": "search:test", "term": term, "geo": geo,
                             "url": "", "reason": "bot_challenge"})
            return []
        original = fa.from_search
        fa.from_search = no_network

        events = []
        state_path = os.path.join(tmp, "state.json")
        cfg = {"verticals": ["home_services"],
               "metros": ["A city", "B city", "C city", "D city"],
               "sources": ["search"], "keep_going": False, "use_sheets": False,
               "delay": 0.0}

        # Stop as soon as the second batch starts.
        seen_batches = []

        def on_event(kind, message, **data):
            events.append((kind, message))
            if kind == "batch":
                seen_batches.append(message)

        pipeline.run(cfg, on_event, lambda: len(seen_batches) >= 2, state_path)

        check("stopped early rather than running every area",
              len(seen_batches) <= 2, f"ran {len(seen_batches)} batches")
        check("said so plainly",
              any("Stopping" in m for k, m in events),
              str([m for k, m in events][-3:]))
        check("wrote state on the way out", os.path.exists(state_path))

        done = pipeline.load_state(state_path)["done_batches"]
        check("only completed batches are marked done",
              len(done) <= 1, str(done))

        # Resuming must pick up at an area that has not been done, not redo one
        # that has. Asserted on which batch it starts with rather than on the
        # lookup count - the stop flag below fires before the first lookup, so
        # counting calls would measure the test's own timing, not the resume.
        seen2 = []
        pipeline.run(cfg, lambda k, m, **d: seen2.append(m) if k == "batch" else None,
                     lambda: len(seen2) >= 1, state_path)
        check("resuming starts somewhere new rather than redoing finished work",
              bool(seen2) and seen2[0] not in [b for b in seen_batches[:len(done)]],
              f"resumed at {seen2[:1]}, already done {done}")
        check("and it did resume rather than finding nothing to do",
              bool(seen2), "no batch was started on resume")
        fa.from_search = original
    finally:
        os.chdir(cwd)
        if tmp in sys.path:
            sys.path.remove(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def test_totals_read_from_disk():
    print("\ntotals")
    tmp = tempfile.mkdtemp()
    cwd = os.getcwd()
    try:
        os.chdir(tmp)
        totals = pipeline.totals_from_disk()
        check("no files yet reads as all zeros",
              totals["agencies"] == 0 and totals["clients"] == 0, str(totals))

        import common
        common.write_csv(pipeline.CLIENTS_CSV, pipeline.CLIENT_FIELDS, [
            {"client_name": "A", "client_domain": "a.com", "agency_domain": "x.com"},
            {"client_name": "B", "client_domain": "", "agency_domain": "x.com"},
        ], quiet=True)
        common.write_csv(pipeline.TAGS_CSV, pipeline.TAG_FIELDS, [
            {"domain": "a.com", "status": "ok", "qualifies": "yes", "tiktok": "no"},
        ], quiet=True)
        totals = pipeline.totals_from_disk()
        check("counts clients", totals["clients"] == 2, str(totals))
        check("counts only those with a domain as checkable",
              totals["clients_with_domain"] == 1, str(totals))
        check("counts a qualifying client", totals["qualifying_clients"] == 1, str(totals))
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in (test_store_upserts_and_merges,
               test_state_is_atomic_and_recovers,
               test_batches_cover_every_pair,
               test_stop_is_prompt_and_saves,
               test_totals_read_from_disk):
        fn()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
