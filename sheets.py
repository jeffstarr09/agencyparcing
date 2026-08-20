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

Setup is a one-time thing - see SETUP.md.
"""

import os
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


def _creds_path():
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "service_account.json")
    if not os.path.exists(path):
        raise SystemExit(
            f"Credentials file not found: {path}\n\n"
            "Either place service_account.json next to this script, or set:\n"
            "    export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service_account.json\n\n"
            "See SETUP.md for how to generate it."
        )
    return path


def connect(sheet_id=None):
    """Open the workbook, creating any missing tabs with their headers."""
    sheet_id = sheet_id or os.environ.get("SHEET_ID", DEFAULT_SHEET_ID)
    creds = Credentials.from_service_account_file(_creds_path(), scopes=SCOPES)
    client = gspread.authorize(creds)

    try:
        book = client.open_by_key(sheet_id)
    except gspread.exceptions.APIError as e:
        if "403" in str(e) or "PERMISSION_DENIED" in str(e):
            sa = Credentials.from_service_account_file(_creds_path()).service_account_email
            raise SystemExit(
                f"Permission denied on sheet {sheet_id}.\n\n"
                f"Share the sheet with this address as an Editor:\n    {sa}\n"
            )
        raise

    existing = {ws.title for ws in book.worksheets()}
    for tab, headers in TABS.items():
        if tab not in existing:
            ws = book.add_worksheet(title=tab, rows=1000, cols=max(len(headers), 26))
            ws.update([headers], "A1")
            ws.freeze(rows=1)
            ws.format("1:1", {"textFormat": {"bold": True}})
        else:
            ws = book.worksheet(tab)
            if not ws.row_values(1):
                ws.update([headers], "A1")
                ws.freeze(rows=1)
                ws.format("1:1", {"textFormat": {"bold": True}})

    # Drive creates a default "Sheet1"; remove it once real tabs exist.
    if "Sheet1" in existing and len(book.worksheets()) > 1:
        try:
            book.del_worksheet(book.worksheet("Sheet1"))
        except Exception:
            pass

    return book


def write(book, tab, rows, upsert=True):
    """
    Push rows into a tab. Rows are dicts; unknown fields are dropped and
    missing fields become blank, so callers don't have to match the schema.

    upsert=True updates rows whose key already exists, appends the rest.
    upsert=False always appends.
    """
    if not rows:
        return 0, 0

    headers = TABS[tab]
    key = KEYS[tab]
    ws = book.worksheet(tab)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    def to_row(d):
        d = dict(d)
        d.setdefault("last_checked", stamp)
        return [str(d.get(h, "") or "") for h in headers]

    if not upsert:
        ws.append_rows([to_row(r) for r in rows], value_input_option="RAW")
        return 0, len(rows)

    key_idx = headers.index(key)
    existing = ws.col_values(key_idx + 1)[1:]  # skip header
    position = {v: i + 2 for i, v in enumerate(existing) if v}

    updates, appends = [], []
    for r in rows:
        k = str(r.get(key, "") or "")
        row = to_row(r)
        if k and k in position:
            rng = f"A{position[k]}:{_col(len(headers))}{position[k]}"
            updates.append({"range": rng, "values": [row]})
        else:
            appends.append(row)

    if updates:
        ws.batch_update(updates, value_input_option="RAW")
    if appends:
        ws.append_rows(appends, value_input_option="RAW")

    return len(updates), len(appends)


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
