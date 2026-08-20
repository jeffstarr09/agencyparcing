#!/usr/bin/env python3
"""
test_sheets_upsert.py - Cover the upsert changes in sheets.write without a network.

Run:  python tests/test_sheets_upsert.py

sheets.write is the one piece of already-working code this project changed, so
the three behaviours that changed get a test each:

  * duplicate keys inside one batch collapse instead of double-appending
  * Clients rows with a blank client_domain upsert on agency_domain + client_name
    instead of appending a fresh row on every run
  * an unknown tab name fails loudly rather than raising KeyError
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sheets  # noqa: E402


class FakeWorksheet:
    """Enough of a gspread worksheet for write() to run against."""

    def __init__(self, headers, rows=None):
        self.values = [list(headers)] + [list(r) for r in (rows or [])]
        self.batched = []
        self.appended = []

    def get_all_values(self):
        return [list(r) for r in self.values]

    def batch_update(self, updates, value_input_option=None):
        self.batched.extend(updates)
        for u in updates:
            at = int("".join(ch for ch in u["range"].split(":")[0] if ch.isdigit()))
            self.values[at - 1] = list(u["values"][0])

    def append_rows(self, rows, value_input_option=None):
        self.appended.extend(rows)
        self.values.extend(list(r) for r in rows)


class FakeBook:
    def __init__(self, ws):
        self._ws = ws

    def worksheet(self, tab):
        return self._ws


PASS, FAIL = [], []


def check(label, condition, detail=""):
    (PASS if condition else FAIL).append(label)
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"\n        {detail}" if detail and not condition else ""))


def test_intrabatch_duplicates_collapse():
    print("\nduplicate keys inside one batch")
    ws = FakeWorksheet(sheets.TABS["Agencies"])
    book = FakeBook(ws)
    rows = [
        {"agency_name": "Alpine", "agency_domain": "alpine.com", "status": "candidate"},
        {"agency_name": "Alpine Digital", "agency_domain": "alpine.com", "status": "checked"},
        {"agency_name": "Northbeam", "agency_domain": "northbeam.com"},
    ]
    updated, added = sheets.write(book, "Agencies", rows)
    check("two rows appended, not three", added == 2, f"added={added}")
    check("nothing updated on an empty tab", updated == 0, f"updated={updated}")
    names = [r[0] for r in ws.appended]
    check("last row for a key wins", "Alpine Digital" in names, f"names={names}")


def test_blank_client_domain_upserts_on_fallback():
    print("\nClients rows with no domain re-run without duplicating")
    headers = sheets.TABS["Clients"]
    ws = FakeWorksheet(headers)
    book = FakeBook(ws)
    row = {"client_name": "Canyon Plumbing Co", "client_domain": "",
           "agency_name": "Alpine Digital", "agency_domain": "alpine.com",
           "confidence": "high"}

    _, added = sheets.write(book, "Clients", [dict(row)])
    check("first run appends", added == 1, f"added={added}")

    updated, added2 = sheets.write(book, "Clients", [dict(row, confidence="medium")])
    check("second run updates, does not append", added2 == 0 and updated == 1,
          f"added={added2} updated={updated}")
    check("only one data row exists", len(ws.values) == 2, f"rows={len(ws.values)}")
    check("the update landed", ws.values[1][headers.index("confidence")] == "medium",
          f"row={ws.values[1]}")


def test_domain_key_still_wins_when_present():
    print("\na populated client_domain is still the key")
    headers = sheets.TABS["Clients"]
    ws = FakeWorksheet(headers)
    book = FakeBook(ws)
    sheets.write(book, "Clients", [{"client_name": "Summit Roofing",
                                    "client_domain": "summitroofing.com",
                                    "agency_domain": "alpine.com"}])
    # Same domain, name written differently - still one client.
    updated, added = sheets.write(book, "Clients", [{"client_name": "Summit Roofing LLC",
                                                     "client_domain": "summitroofing.com",
                                                     "agency_domain": "alpine.com"}])
    check("matched on domain despite a different name", updated == 1 and added == 0,
          f"updated={updated} added={added}")


def test_unknown_tab_fails_loudly():
    print("\nunknown tab")
    ws = FakeWorksheet(sheets.TABS["Agencies"])
    try:
        sheets.write(FakeBook(ws), "Prospects", [{"agency_domain": "x.com"}])
        check("raises SystemExit", False, "no exception raised")
    except SystemExit as e:
        check("raises SystemExit naming the known tabs", "Agencies" in str(e), str(e))
    except KeyError as e:
        check("raises SystemExit not KeyError", False, f"KeyError: {e}")


def test_row_key_shapes():
    print("\n_row_key")
    check("primary key lowercased",
          sheets._row_key("Agencies", {"agency_domain": "Alpine.COM"}) == "alpine.com")
    check("blank everything yields no key",
          sheets._row_key("Clients", {}) == "")
    check("Ad Tags has no fallback",
          sheets._row_key("Ad Tags", {"client_name": "x"}) == "")
    check("Clients fallback combines agency and name",
          sheets._row_key("Clients", {"agency_domain": "a.com", "client_name": "B"})
          == "a.com::b")


if __name__ == "__main__":
    for fn in (test_intrabatch_duplicates_collapse,
               test_blank_client_domain_upserts_on_fallback,
               test_domain_key_still_wins_when_present,
               test_unknown_tab_fails_loudly,
               test_row_key_shapes):
        fn()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
