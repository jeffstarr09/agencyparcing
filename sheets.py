#!/usr/bin/env python3
"""
sheets.py - Google Sheets output for the TikTok-gap prospecting pipeline.

Creates and maintains three tabs in one workbook:

    Agencies  - the shops you're evaluating, and whether they mention TikTok
    Clients   - brands parsed off each agency's client page
    Ad Tags   - pixel_check.py results, one row per client domain

Tabs are created on first run if missing, so you never have to set them up
by hand. Writes are upsert-by-key, not append: re-running a domain updates
its existing row instead of duplicating it.

Setup is a one-time thing - see README.md.
"""

import os
import sys
from datetime import datetime, timezone

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    raise SystemExit(
        "Missing dependencies. Run:\n"
        "    pip install gspread google-auth\n"
    )

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# Your workbook. Override with the SHEET_ID env var if you make a copy.
DEFAULT_SHEET_ID = "1TpvjeWyDt4oGmc7bqtEaq8zf9pqWiPmzVULYxcwO3ow"

TABS = {
    "Agencies": [
        "agency_name", "agency_domain", "vertical", "hq_location",
        "employee_count", "mentions_tiktok", "tiktok_evidence",
        "client_page_url", "clients_found", "status", "notes", "last_checked",
    ],
    "Clients": [
        "client_name", "client_domain", "agency_name", "agency_domain",
        "vertical", "source", "confidence", "last_checked",
    ],
    "Ad Tags": [
        "domain", "client_name", "agency_name", "resolved_url", "http_status",
        "status", "qualifies", "tiktok", "meta", "google_ads", "floodlight",
        "pinterest", "snapchat", "gtm_ids", "ga4_ids",
        "tiktok_evidence", "meta_evidence", "google_ads_evidence",
        "floodlight_evidence", "pinterest_evidence", "snapchat_evidence",
        "last_checked",
    ],
}

# Which column identifies a row uniquely, per tab. Used for upserts.
KEYS = {"Agencies": "agency_domain", "Clients": "client_domain", "Ad Tags": "domain"}

# When the primary key is blank, fall back to these columns joined together.
# This exists for Clients: parse_clients.py legitimately produces rows with a
# name and no resolvable domain, and without a fallback every re-run would
# append "Acme Plumbing" again instead of updating the row already there.
KEY_FALLBACKS = {"Clients": ["agency_domain", "client_name"]}


def _creds_path():
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "service_account.json")
    if not os.path.exists(path):
        raise SystemExit(
            f"Credentials file not found: {path}\n\n"
            "Either place service_account.json next to this script, or set:\n"
            "    export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service_account.json\n\n"
            "See README.md step 2 for how to generate it."
        )
    return path


def _service_account_email(path):
    """Read the robot address straight out of the key file, for error messages."""
    try:
        import json
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("client_email", "(client_email missing from key file)")
    except Exception:
        return "(could not read client_email from the key file)"


def connect(sheet_id=None):
    """Open the workbook, creating any missing tabs with their headers."""
    sheet_id = sheet_id or os.environ.get("SHEET_ID") or DEFAULT_SHEET_ID
    path = _creds_path()
    creds = Credentials.from_service_account_file(path, scopes=SCOPES)
    client = gspread.authorize(creds)

    try:
        book = client.open_by_key(sheet_id)
    except gspread.exceptions.APIError as e:
        text = str(e)
        if "403" in text or "PERMISSION_DENIED" in text:
            raise SystemExit(
                f"Permission denied on sheet {sheet_id}.\n\n"
                f"Share the sheet with this address as an Editor:\n"
                f"    {_service_account_email(path)}\n"
            )
        if "404" in text or "NOT_FOUND" in text:
            raise SystemExit(
                f"No sheet with id {sheet_id}.\n\n"
                "Check SHEET_ID, or pass --sheet-id. The id is the long string in\n"
                "the sheet URL between /d/ and /edit.\n"
            )
        raise

    existing = {ws.title for ws in book.worksheets()}
    for tab, headers in TABS.items():
        if tab not in existing:
            ws = book.add_worksheet(title=tab, rows=1000, cols=max(len(headers), 26))
            _install_header(ws, headers)
        else:
            ws = book.worksheet(tab)
            current = ws.row_values(1)
            if not current:
                _install_header(ws, headers)
            elif current != headers:
                # A drifted header silently misfiles every subsequent write, so
                # say so rather than writing columns into the wrong places.
                missing = [h for h in headers if h not in current]
                raise SystemExit(
                    f"Tab '{tab}' has an unexpected header row.\n"
                    f"  expected: {headers}\n"
                    f"  found:    {current}\n"
                    + (f"  missing:  {missing}\n" if missing else "")
                    + "\nFix the header row, or rename the tab and let this script "
                      "recreate it.\n"
                )

    # Drive creates a default "Sheet1"; remove it once real tabs exist.
    if "Sheet1" in existing and len(book.worksheets()) > 1:
        try:
            book.del_worksheet(book.worksheet("Sheet1"))
        except Exception:
            pass

    return book


def _install_header(ws, headers):
    ws.update([headers], "A1")
    ws.freeze(rows=1)
    ws.format("1:1", {"textFormat": {"bold": True}})


def _row_key(tab, d):
    """
    The upsert identity for a row. Primary key when populated, otherwise the
    tab's fallback columns joined. Returns "" when neither is usable, which
    means "append, and accept that a re-run may duplicate it".
    """
    primary = str(d.get(KEYS[tab], "") or "").strip().lower()
    if primary:
        return primary
    fallback = KEY_FALLBACKS.get(tab)
    if not fallback:
        return ""
    parts = [str(d.get(c, "") or "").strip().lower() for c in fallback]
    return "::".join(parts) if any(parts) else ""


def write(book, tab, rows, upsert=True):
    """
    Push rows into a tab. Rows are dicts; unknown fields are dropped and
    missing fields become blank, so callers don't have to match the schema.

    upsert=True updates rows whose key already exists, appends the rest.
    upsert=False always appends.

    Returns (updated_count, appended_count).
    """
    if tab not in TABS:
        raise SystemExit(f"Unknown tab '{tab}'. Known tabs: {list(TABS)}")
    if not rows:
        return 0, 0

    headers = TABS[tab]
    ws = book.worksheet(tab)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    def to_row(d):
        d = dict(d)
        d.setdefault("last_checked", stamp)
        return [str(d.get(h, "") or "") for h in headers]

    # Collapse duplicates inside this batch first. Without this, two rows for
    # the same key in one call would both be appended, or would issue two
    # conflicting writes to the same range. Last one wins, matching the
    # "re-running updates" contract.
    merged, order, keyless = {}, [], []
    for r in rows:
        k = _row_key(tab, r)
        if not k:
            keyless.append(r)
            continue
        if k not in merged:
            order.append(k)
        merged[k] = r
    batch = [merged[k] for k in order]

    if not upsert:
        ws.append_rows([to_row(r) for r in batch + keyless], value_input_option="RAW")
        return 0, len(batch) + len(keyless)

    # One read of the whole tab: the fallback key needs more than the key column,
    # and this is fewer API round-trips than fetching columns one at a time.
    values = ws.get_all_values()
    position = {}
    if len(values) > 1:
        sheet_headers = values[0]
        idx = {h: i for i, h in enumerate(sheet_headers)}
        for offset, row in enumerate(values[1:], start=2):
            existing = {h: (row[i] if i < len(row) else "") for h, i in idx.items()}
            k = _row_key(tab, existing)
            if k:
                position.setdefault(k, offset)

    last_col = _col(len(headers))
    updates, appends = [], []
    for k, r in zip(order, batch):
        row = to_row(r)
        if k in position:
            at = position[k]
            updates.append({"range": f"A{at}:{last_col}{at}", "values": [row]})
        else:
            appends.append(row)
    for r in keyless:
        appends.append(to_row(r))

    if updates:
        ws.batch_update(updates, value_input_option="RAW")
    if appends:
        ws.append_rows(appends, value_input_option="RAW")
    if keyless:
        print(f"  note: {len(keyless)} row(s) had no usable key and were appended; "
              f"re-running them will duplicate.", file=sys.stderr)

    return len(updates), len(appends)


def read_tab(book, tab):
    """Read a tab back as a list of dicts. Used by score_agencies.py."""
    if tab not in TABS:
        raise SystemExit(f"Unknown tab '{tab}'. Known tabs: {list(TABS)}")
    values = book.worksheet(tab).get_all_values()
    if len(values) < 2:
        return []
    headers = values[0]
    out = []
    for row in values[1:]:
        rec = {h: (row[i] if i < len(row) else "") for i, h in enumerate(headers)}
        if any(v.strip() for v in rec.values()):
            out.append(rec)
    return out


def _col(n):
    """1 -> A, 27 -> AA."""
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


if __name__ == "__main__":
    book = connect()
    print(f"Connected: {book.title}")
    print(f"URL: {book.url}")
    for ws in book.worksheets():
        print(f"  tab '{ws.title}' - {len(ws.col_values(1)) - 1} data rows")
