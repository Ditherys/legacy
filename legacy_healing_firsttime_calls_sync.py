"""
Legacy Healing Center — First-Time Inbound Calls → Google Sheets sync.

Filters applied (matching CTM export UI):
  - Type:              Inbound call (direction=inbound)
  - First time contact: is_new_caller=true
  - Philippine Reps:   agent.email ends with @allianceglobalsolutions.com
  - Status:            Answered, Hangup, No Answer (all statuses — no filter)

Sheet columns:
  A  Customer #
  B  Call Status
  C  Date
  D  Time
  E  Agent
  F  custom[activity_notes]

Upsert key: composite of (Customer #, Date, Time) — columns A, C, D.
Run every 5 minutes via GitHub Actions cron or manually.
Caches daily call data to output/legacy_healing_cache/ to avoid
re-fetching completed days; today's data is always re-fetched fresh.
"""

import base64
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TIMEZONE_NAME = "America/New_York"
EMAIL_DOMAIN = "@allianceglobalsolutions.com"
OUTPUT_DIR = Path("output")
CACHE_DIR = OUTPUT_DIR / "legacy_healing_cache"

SHEET_HEADERS = [
    "Customer #",
    "Call Status",
    "Date",
    "Time",
    "Agent",
    "custom[activity_notes]",
    "",          # G — reserved for user
    "Last Synced",
]

# Upsert key: (Customer #, Date, Time) — column indices 0, 2, 3
UPSERT_KEY_COLS = (0, 2, 3)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
NUM_COLS = len(SHEET_HEADERS)  # 8 → column H
COL_LETTER = "H"
RANGE_FULL = "A:H"


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def load_dotenv(path):
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def require_env(*names, default=None):
    for name in names:
        v = os.getenv(name)
        if v:
            return v
    if default is not None:
        return default
    raise SystemExit(f"Missing required env var: {' or '.join(names)}")


# ---------------------------------------------------------------------------
# CTM API
# ---------------------------------------------------------------------------

def ctm_credentials():
    return {
        "host": require_env("CTM_API_HOST", default="https://api.calltrackingmetrics.com").rstrip("/"),
        "access_key": require_env("CTM_ACCESS_KEY", "CTM_API_KEY"),
        "secret_key": require_env("CTM_SECRET_KEY", "CTM_API_SECRET"),
        "account_id": require_env("CTM_ACCOUNT_ID"),
    }


def ctm_get(creds, path, params):
    token = base64.b64encode(f"{creds['access_key']}:{creds['secret_key']}".encode()).decode()
    url = f"{creds['host']}{path}?" + urlencode({k: v for k, v in params.items() if v not in (None, "")})
    req = Request(url, headers={"Authorization": f"Basic {token}", "Accept": "application/json"})
    try:
        with urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"CTM API HTTP {exc.code}: {url}\n{detail[:500]}") from exc
    except URLError as exc:
        raise SystemExit(f"CTM API unreachable: {url}\n{exc}") from exc


def fetch_day_calls(creds, day, force_refresh=False):
    """Fetch all inbound first-time calls for a single day. Caches to disk."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"calls_{day.isoformat()}.json"

    today = datetime.now(ZoneInfo(TIMEZONE_NAME)).date()
    use_cache = cache_path.exists() and not force_refresh and day < today

    if use_cache:
        return json.loads(cache_path.read_text(encoding="utf-8"))

    path = f"/api/v1/accounts/{creds['account_id']}/calls.json"
    calls = []
    after = None
    page = 1
    while True:
        params = {
            "per_page": 100,
            "start_date": day.isoformat(),
            "end_date": day.isoformat(),
            "direction": "inbound",
            "is_new_caller": "true",
        }
        if after:
            params["after"] = after
        data = ctm_get(creds, path, params)
        batch = data.get("calls") or []
        calls.extend(batch)
        print(f"  {day.isoformat()} page {page}: {len(calls)} calls so far")
        after = data.get("after")
        if not data.get("next_page") or not after or not batch:
            break
        page += 1

    if day < today:
        cache_path.write_text(json.dumps(calls), encoding="utf-8")
    return calls


def is_philippine_rep(call):
    email = ((call.get("agent") or {}).get("email") or "").strip().lower()
    return email.endswith(EMAIL_DOMAIN)


def activity_notes(call):
    fields = call.get("custom_fields") or {}
    if isinstance(fields, dict):
        return fields.get("activity_notes", "") or ""
    return ""


def format_call_row(call):
    """Convert a CTM call object to a 6-column sheet row."""
    agent_email = ((call.get("agent") or {}).get("email") or "").strip()

    called_at = call.get("called_at") or ""
    call_date = ""
    call_time = ""
    if called_at:
        try:
            dt = datetime.fromisoformat(called_at.replace("Z", "+00:00"))
            est = dt.astimezone(ZoneInfo(TIMEZONE_NAME))
            call_date = est.strftime("%Y-%m-%d")
            call_time = est.strftime("%I:%M:%S %p")
        except ValueError:
            call_date = called_at[:10]
            call_time = called_at[11:19]

    status = call.get("status") or call.get("dial_status") or ""
    customer_number = (call.get("caller_number_format") or call.get("caller_number") or "").strip()

    return [
        customer_number,           # Customer #
        status,                    # Call Status
        call_date,                 # Date
        call_time,                 # Time
        agent_email,               # Agent
        activity_notes(call),      # custom[activity_notes]
    ]


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

def find_google_credentials():
    b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
    if b64:
        try:
            return {"type": "json", "value": json.loads(base64.b64decode(b64).decode())}
        except Exception:
            pass
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        try:
            return {"type": "json", "value": json.loads(raw)}
        except Exception:
            pass
    path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    if path and Path(path).exists():
        return {"type": "file", "value": path}
    matches = sorted(Path(".").glob("secrets/*.json"))
    if len(matches) == 1:
        return {"type": "file", "value": str(matches[0])}
    raise SystemExit(
        "No Google credentials found. Set GOOGLE_SERVICE_ACCOUNT_JSON_B64, "
        "GOOGLE_SERVICE_ACCOUNT_JSON, or GOOGLE_SERVICE_ACCOUNT_FILE."
    )


def build_sheets_service(creds_source):
    if creds_source["type"] == "json":
        creds = service_account.Credentials.from_service_account_info(creds_source["value"], scopes=SCOPES)
    else:
        creds = service_account.Credentials.from_service_account_file(creds_source["value"], scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def sheet_range(sheet_name, a1):
    escaped = sheet_name.replace("'", "''")
    return f"'{escaped}'!{a1}"


def get_values(service, spreadsheet_id, sheet_name, a1):
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=sheet_range(sheet_name, a1),
    ).execute()
    return result.get("values", [])


def ensure_headers(service, spreadsheet_id, sheet_name):
    existing = get_values(service, spreadsheet_id, sheet_name, "A1:H1")
    if existing and existing[0] == SHEET_HEADERS:
        return
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=sheet_range(sheet_name, "A1"),
        valueInputOption="RAW",
        body={"values": [SHEET_HEADERS]},
    ).execute()
    print("Headers written to row 1.")


def _row_key(row):
    """Composite key from (Customer #, Date, Time)."""
    padded = list(row) + [""] * (NUM_COLS - len(row))
    return (
        str(padded[UPSERT_KEY_COLS[0]]).strip().lower(),
        str(padded[UPSERT_KEY_COLS[1]]).strip().lower(),
        str(padded[UPSERT_KEY_COLS[2]]).strip().lower(),
    )


def upsert_rows(service, spreadsheet_id, sheet_name, new_rows, sync_timestamp):
    """Update rows with matching composite key in-place; append new ones.
    Last Synced (col H) is only stamped on new rows, never overwritten.
    """
    existing = get_values(service, spreadsheet_id, sheet_name, f"A2:{COL_LETTER}")

    key_to_row = {}
    for idx, row in enumerate(existing):
        key = _row_key(row)
        if any(k for k in key):
            key_to_row[key] = idx + 2  # +2: header row + 0-based offset

    updates = []
    appends = []
    for row in new_rows:
        key = _row_key(row)
        if key in key_to_row:
            row_num = key_to_row[key]
            # Update columns A:F only — leave G and Last Synced (H) untouched
            updates.append({
                "range": sheet_range(sheet_name, f"A{row_num}:F{row_num}"),
                "values": [row[:6]],
            })
        else:
            # New row: stamp Last Synced in col H
            appends.append(row[:6] + ["", sync_timestamp])

    if updates:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "RAW", "data": updates},
        ).execute()

    if appends:
        service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=sheet_range(sheet_name, f"A2:{COL_LETTER}"),
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": appends},
        ).execute()

    return len(updates), len(appends)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    load_dotenv(Path(".env.local"))

    force_refresh = os.getenv("FORCE_REFRESH", "false").lower() == "true"
    spreadsheet_id = require_env("LEGACY_HEALING_SPREADSHEET_ID", "GOOGLE_SPREADSHEET_ID")
    sheet_name = os.getenv("LEGACY_HEALING_SHEET_NAME", "Calls")

    creds = ctm_credentials()
    today = datetime.now(ZoneInfo(TIMEZONE_NAME)).date()
    start_date = date(today.year, today.month, 1)  # month-to-date

    print(f"Fetching Legacy Healing first-time inbound calls {start_date} to {today} (EST)")

    all_rows = []
    current = start_date
    while current <= today:
        calls = fetch_day_calls(creds, current, force_refresh=force_refresh)
        phil_calls = [c for c in calls if is_philippine_rep(c)]
        for call in phil_calls:
            all_rows.append(format_call_row(call))
        print(f"  {current.isoformat()}: {len(calls)} first-time inbound, {len(phil_calls)} Philippine rep calls")
        current += timedelta(days=1)

    # Sort ascending by date then time so the sheet reads chronologically
    def _time_key(t):
        for fmt in ("%I:%M:%S %p", "%I:%M %p"):
            try:
                return datetime.strptime(t, fmt)
            except ValueError:
                continue
        return datetime.min

    all_rows.sort(key=lambda r: (r[2], _time_key(r[3])))

    print(f"Total rows to sync: {len(all_rows)}")

    sync_timestamp = datetime.now(ZoneInfo(TIMEZONE_NAME)).strftime("%Y-%m-%d %I:%M:%S %p")

    google_creds = find_google_credentials()
    service = build_sheets_service(google_creds)

    ensure_headers(service, spreadsheet_id, sheet_name)
    updated, appended = upsert_rows(service, spreadsheet_id, sheet_name, all_rows, sync_timestamp)
    print(f"Done — updated: {updated}, appended: {appended}")


if __name__ == "__main__":
    main()
