import base64
import csv
import json
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, time as dt_time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# --- CTM config ---
START_DATE = date(2026, 5, 1)
END_DATE = date(2026, 5, 31)
EMAIL_DOMAIN = "@allianceglobalsolutions.com"
TIMEZONE_NAME = "America/New_York"
TIMEZONE_LABEL = "EST"
OUTPUT_DIR = Path("output")
CACHE_DIR = OUTPUT_DIR / "may_cache"
REPORT_CSV = OUTPUT_DIR / "ctm_alliance_may_2026_report.csv"
REPORT_JSON = OUTPUT_DIR / "ctm_alliance_may_2026_report.json"

# --- Google Sheets config ---
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
DEFAULT_SPREADSHEET_ID = "1vMJwoOoFC9jg0mOAOwT2i1iWSVNh16PsQ89U2Icr2AU"
DEFAULT_AGENT_KEY_SHEET = "Primary Key"

# --- Manual May 2026 data for verification (calls, transfers) ---
# Names are in "Last, First" format as provided manually.
# The script will try to match against both mapped KPI names and original CTM names.
VERIFY_AGENTS = [
    ("Aban, Ma Melanie",         164, 28),
    ("Angel, Byron Jake",         202, 36),
    ("Bernardo, Sean",            183, 31),
    ("Buranis, Shenelyn",          93, 16),
    ("Dujale, John Vincent",       73, 12),
    ("Galong, Julia Mae",          90, 16),
    ("Lajo, Ella Mae",            109, 20),
    ("Lopez, Maria Nelia",        217, 23),
    ("Lumactod, Nikki",           170, 33),
    ("Magbanua, Rubie",           138, 28),
    ("Navarro, Joanne Stephanie", 124, 25),
    ("Padullano, Larinz",         254, 46),
    ("Perez, Karen",              205, 27),
    ("Rapis, Samuel",             234, 40),
    ("Regla, Ma Cecilia",         255, 36),
    ("Sagun, Rae",                140, 26),
    ("Villarta, Remington",        89, 11),
]

# Talk/hold/AHT for Melanie Aban only (used for endpoint verification)
VERIFY_TALK_HMS = "6:45:05"
VERIFY_HOLD_HMS = "0:46:19"
VERIFY_AHT_HMS = "0:02:45"


# ---------------------------------------------------------------------------
# Env / config helpers
# ---------------------------------------------------------------------------

def load_dotenv(path):
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_first_env(*names, required=True, default=None):
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    if required:
        raise SystemExit(f"Missing required environment variable: {' or '.join(names)}")
    return default


def get_credentials():
    return {
        "api_host": get_first_env("CTM_API_HOST", required=False, default="https://api.calltrackingmetrics.com").rstrip("/"),
        "access_key": get_first_env("CTM_ACCESS_KEY", "CTM_API_KEY"),
        "secret_key": get_first_env("CTM_SECRET_KEY", "CTM_API_SECRET"),
        "account_id": get_first_env("CTM_ACCOUNT_ID"),
    }


# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------

def find_google_credentials_source():
    """Returns a credentials source dict, or None if nothing is configured."""
    json_b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
    if json_b64:
        try:
            decoded = base64.b64decode(json_b64).decode("utf-8")
            return {"type": "json", "label": "GOOGLE_SERVICE_ACCOUNT_JSON_B64", "value": json.loads(decoded)}
        except (ValueError, json.JSONDecodeError):
            return None

    json_text = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if json_text:
        try:
            return {"type": "json", "label": "GOOGLE_SERVICE_ACCOUNT_JSON", "value": json.loads(json_text)}
        except json.JSONDecodeError:
            return None

    explicit = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    if explicit:
        path = Path(explicit)
        if path.exists():
            return {"type": "file", "label": str(path), "value": str(path)}
        return None

    matches = sorted(Path(".").glob("secrets/*.json"))
    if len(matches) == 1:
        return {"type": "file", "label": str(matches[0]), "value": str(matches[0])}

    return None


def build_sheets_service(credentials_source):
    if credentials_source["type"] == "json":
        creds = service_account.Credentials.from_service_account_info(
            credentials_source["value"], scopes=SCOPES
        )
    else:
        creds = service_account.Credentials.from_service_account_file(
            credentials_source["value"], scopes=SCOPES
        )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def get_sheet_values(service, spreadsheet_id, sheet_name, a1_range):
    escaped = sheet_name.replace("'", "''")
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"'{escaped}'!{a1_range}")
        .execute()
    )
    return result.get("values", [])


def pad(values, length):
    if len(values) >= length:
        return values
    return values + [""] * (length - len(values))


def normalize_value(value):
    return " ".join(str(value or "").strip().lower().split())


def normalize_header(value):
    return re.sub(r"[^a-z0-9]", "", normalize_value(value))


def column_index(headers, aliases):
    normalized = [normalize_header(h) for h in headers]
    for alias in aliases:
        try:
            return normalized.index(normalize_header(alias))
        except ValueError:
            continue
    return None


def build_agent_name_map(values):
    """
    Reads the Primary Key Google Sheet tab and returns a mapping of
    CTM email / CTM name → KPI display name.

    Expected columns (flexible header matching):
      CTM Email | CTM Agent Name | KPI Agent Name
    """
    if not values:
        return {"by_email": {}, "by_name": {}, "rows": 0, "available": False}

    headers = values[0]
    email_idx = column_index(headers, ["CTM Email", "Email", "ctm_email"])
    ctm_name_idx = column_index(headers, ["CTM_Agent", "CTM Agent", "CTM Agent Name", "CTM Name", "ctm_agent_name"])
    kpi_name_idx = column_index(headers, ["KPI Agent Name", "KPI Name", "Agent", "Agents", "kpi_agent_name"])

    if kpi_name_idx is None:
        raise SystemExit(
            "Primary Key sheet needs a KPI Agent Name column. "
            "Recommended headers: CTM Email, CTM Agent Name, KPI Agent Name."
        )

    by_email = {}
    by_name = {}
    rows = 0
    for raw_row in values[1:]:
        row = pad(raw_row, len(headers))
        kpi_name = row[kpi_name_idx].strip() if kpi_name_idx < len(row) else ""
        if not kpi_name:
            continue

        if email_idx is not None and email_idx < len(row):
            email = normalize_value(row[email_idx])
            if email:
                by_email[email] = kpi_name

        if ctm_name_idx is not None and ctm_name_idx < len(row):
            ctm_name = normalize_value(row[ctm_name_idx])
            if ctm_name:
                by_name[ctm_name] = kpi_name

        rows += 1

    return {"by_email": by_email, "by_name": by_name, "rows": rows, "available": True}


def apply_agent_name_map(rows, agent_name_map):
    """
    Replaces agent_name with the KPI display name from the mapping.
    Saves the original CTM name in ctm_agent_name for reference.
    Matches by email first, then by CTM name as fallback.
    """
    mapped = 0
    for row in rows:
        original = row["agent_name"]
        mapped_name = agent_name_map["by_email"].get(normalize_value(row["agent_email"]))
        if not mapped_name:
            mapped_name = agent_name_map["by_name"].get(normalize_value(original))
        if mapped_name and mapped_name != original:
            row["ctm_agent_name"] = original
            row["agent_name"] = mapped_name
            mapped += 1
    return mapped


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def hms(seconds):
    total_seconds = int(round(float(seconds or 0)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def hms_to_seconds(value):
    if not value:
        return 0
    parts = str(value).strip().split(":")
    if len(parts) != 3:
        return 0
    h, m, s = parts
    return int(h) * 3600 + int(m) * 60 + int(s)


# ---------------------------------------------------------------------------
# CTM API helpers
# ---------------------------------------------------------------------------

def date_to_epoch(day, end_of_day=False):
    local_time = dt_time(23, 59, 59) if end_of_day else dt_time(0, 0, 0)
    local_dt = datetime.combine(day, local_time, tzinfo=ZoneInfo(TIMEZONE_NAME))
    return int(local_dt.timestamp())


def build_url(credentials, path, params):
    query = urlencode({key: value for key, value in params.items() if value not in (None, "")})
    url = f"{credentials['api_host']}{path}"
    return f"{url}?{query}" if query else url


def api_get(credentials, path, params):
    url = build_url(credentials, path, params)
    token = f"{credentials['access_key']}:{credentials['secret_key']}".encode("utf-8")
    auth_header = base64.b64encode(token).decode("ascii")
    request = Request(
        url,
        headers={
            "Authorization": f"Basic {auth_header}",
            "Accept": "application/json",
            "User-Agent": "legacy-ctm-may-alliance-report/1.0",
        },
    )
    try:
        with urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8")), url
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"CTM API returned HTTP {exc.code} for {url}\n{detail[:1000]}") from exc
    except URLError as exc:
        raise SystemExit(f"Could not reach CTM API for {url}\n{exc}") from exc


# ---------------------------------------------------------------------------
# CTM data fetchers
# ---------------------------------------------------------------------------

def fetch_all_users(credentials):
    path = f"/api/v1/accounts/{credentials['account_id']}/users.json"
    page = 1
    users = []
    while True:
        payload, _url = api_get(credentials, path, {"per_page": 100, "page": page})
        batch = payload.get("users") or []
        if isinstance(batch, dict):
            batch = list(batch.values())
        users.extend(batch)
        if not payload.get("next_page"):
            return users
        page += 1


def alliance_user_rows(users):
    rows = []
    for user in users:
        email = (user.get("email") or "").strip().lower()
        if not email.endswith(EMAIL_DOMAIN):
            continue
        name = (user.get("name") or "").strip()
        if not name:
            name = " ".join(piece for piece in [user.get("first_name"), user.get("last_name")] if piece).strip()
        rows.append({
            "agent_name": name,
            "agent_email": email,
            "ctm_agent_id": (user.get("id") or "").strip(),
        })
    return sorted(rows, key=lambda row: (row["agent_name"].lower(), row["agent_email"]))


def fetch_utilization(credentials):
    path = f"/api/v1/accounts/{credentials['account_id']}/agents/utilization.json"
    params = {
        "start_time": date_to_epoch(START_DATE, end_of_day=False),
        "end_time": date_to_epoch(END_DATE, end_of_day=True),
        "timezone": TIMEZONE_LABEL,
        "interval": "day",
        "statistic": "occupancy",
        "view_by": "agent",
        "es": "1",
    }
    return api_get(credentials, path, params)


def metric_by_email(payload, metric_name):
    users = {str(key): value for key, value in (payload.get("users") or {}).items()}
    rows = (payload.get("metrics") or {}).get(metric_name) or []
    by_email = defaultdict(lambda: {"count": 0, "total": 0.0})
    for row in rows:
        user_id = str(row.get("user_id") or "").strip()
        email = ((users.get(user_id) or {}).get("email") or "").strip().lower()
        if not email:
            continue
        by_email[email]["count"] += int(float(row.get("count") or 0))
        by_email[email]["total"] += float(row.get("total") or 0)
    return by_email


def day_range(start_day, end_day):
    current = start_day
    while current <= end_day:
        yield current
        current += timedelta(days=1)


def fetch_day_answered_inbound_calls(credentials, day):
    # direction=inbound excludes CTM form submissions (direction=form), SMS, and outbound.
    # status=answered ensures only picked-up calls are counted.
    # If the account manager means click-to-call tracking numbers tied to web forms,
    # ask them for the specific tracking source name so it can be filtered out.
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"calls_inbound_answered_{day.isoformat()}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    path = f"/api/v1/accounts/{credentials['account_id']}/calls.json"
    calls = []
    after = None
    page = 1
    while True:
        params = {
            "per_page": 50,
            "start_date": day.isoformat(),
            "end_date": day.isoformat(),
            "direction": "inbound",
            "status": "answered",
        }
        if after:
            params["after"] = after

        payload, _url = api_get(credentials, path, params)
        batch = payload.get("calls") or []
        calls.extend(batch)
        print(f"  {day.isoformat()} page {page}: {len(calls)} rows")

        after = payload.get("after")
        if not payload.get("next_page") or not after or not batch:
            break
        page += 1

    cache_path.write_text(json.dumps(calls), encoding="utf-8")
    return calls


def raw_counts_from_calls(credentials, users):
    sid_to_email = {
        (user.get("id") or "").strip(): (user.get("email") or "").strip().lower()
        for user in users
        if (user.get("email") or "").strip().lower().endswith(EMAIL_DOMAIN)
    }
    call_counts = defaultdict(lambda: {"answered": 0})
    # Transfers count BOTH first-time and repeat callers (as requested).
    transfer_counts = defaultdict(lambda: {"all": 0, "first_time": 0, "repeat": 0})
    total_rows = 0

    for day in day_range(START_DATE, END_DATE):
        calls = fetch_day_answered_inbound_calls(credentials, day)
        total_rows += len(calls)
        for call in calls:
            assigned_email = ((call.get("agent") or {}).get("email") or "").strip().lower()
            was_transferred = bool(call.get("transfers"))
            if assigned_email.endswith(EMAIL_DOMAIN) and not was_transferred:
                call_counts[assigned_email]["answered"] += 1

            from_emails = set()
            for transfer in call.get("transfers") or []:
                from_email = sid_to_email.get((transfer.get("from") or "").strip())
                if from_email:
                    from_emails.add(from_email)

            for email in from_emails:
                transfer_counts[email]["all"] += 1
                if call.get("is_new_caller"):
                    transfer_counts[email]["first_time"] += 1
                else:
                    transfer_counts[email]["repeat"] += 1

    return call_counts, transfer_counts, total_rows


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _name_tokens(name):
    """Normalize a name to a frozenset of lowercase tokens for flexible matching.
    Handles both 'First Last' and 'Last, First' formats."""
    return frozenset(re.sub(r"[^a-z ]", "", name.lower()).split())


def _find_row(rows, verify_name):
    """Find a report row by name, tolerating 'Last, First' vs 'First Last' differences."""
    tokens = _name_tokens(verify_name)
    for row in rows:
        if _name_tokens(row["agent_name"]) == tokens:
            return row
        if _name_tokens(row.get("ctm_agent_name", "")) == tokens:
            return row
    return None


def print_verification(rows, call_counts, transfer_counts, talk_by_email, hold_by_email, util_inbound_by_email):
    print()
    print("=" * 72)
    print("MAY 2026 VERIFICATION — manual data vs script output")
    print(f"  {'Agent':<30} {'Exp':>5} {'Got':>5} {'Calls':>7}  {'Exp':>5} {'Got':>5} {'Xfrs':>7}")
    print(f"  {'-'*30} {'-'*5} {'-'*5} {'-'*7}  {'-'*5} {'-'*5} {'-'*7}")

    all_match = True
    not_found = []

    for verify_name, exp_calls, exp_transfers in VERIFY_AGENTS:
        agent_row = _find_row(rows, verify_name)
        if not agent_row:
            not_found.append(verify_name)
            all_match = False
            continue

        email = agent_row["agent_email"]
        got_calls = call_counts[email]["answered"]
        got_transfers = transfer_counts[email]["all"]
        calls_tag = "✓" if got_calls == exp_calls else "✗"
        xfrs_tag = "✓" if got_transfers == exp_transfers else "✗"
        if calls_tag == "✗" or xfrs_tag == "✗":
            all_match = False

        display = agent_row["agent_name"]
        print(f"  {display:<30} {exp_calls:>5} {got_calls:>5} {calls_tag:>7}  {exp_transfers:>5} {got_transfers:>5} {xfrs_tag:>7}")

    print("=" * 72)

    if not_found:
        print("  NOT FOUND (check Primary Key name mapping):")
        for name in not_found:
            print(f"    - {name}")

    if all_match and not not_found:
        print("  All agents MATCH ✓")
    else:
        print("  Some agents have mismatches — check ✗ rows above.")

    # Detailed endpoint breakdown for Melanie Aban only (talk/hold/AHT)
    melanie_row = _find_row(rows, "Aban, Ma Melanie")
    if melanie_row:
        email = melanie_row["agent_email"]
        raw_calls = call_counts[email]["answered"]
        util_inbound = util_inbound_by_email[email]["count"]
        talk_s = int(round(talk_by_email[email]["total"]))
        hold_s = int(round(hold_by_email[email]["total"]))
        exp_talk_s = hms_to_seconds(VERIFY_TALK_HMS)
        exp_hold_s = hms_to_seconds(VERIFY_HOLD_HMS)
        exp_aht_s = hms_to_seconds(VERIFY_AHT_HMS)

        def tag(actual, expected):
            return "MATCH ✓" if actual == expected else f"DIFF  (expected {expected})"

        print()
        print("  ENDPOINT DETAIL: Melanie Aban — talk/hold/AHT source check")
        print(f"    talk : {hms(talk_s):>10}  {tag(talk_s, exp_talk_s)}")
        print(f"    hold : {hms(hold_s):>10}  {tag(hold_s, exp_hold_s)}")
        for label, n in [("raw calls.json", raw_calls), ("util inbound_calls", util_inbound)]:
            aht_s = round((talk_s + hold_s) / n) if n else 0
            print(f"    aht ({label} / {n}): {hms(aht_s):>10}  {tag(aht_s, exp_aht_s)}")
    print("=" * 72)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    load_dotenv(Path(".env.local"))
    OUTPUT_DIR.mkdir(exist_ok=True)

    credentials = get_credentials()

    print("Fetching CTM users...")
    users = fetch_all_users(credentials)
    agents = alliance_user_rows(users)
    print(f"Found {len(agents)} Alliance agents")

    print("Fetching utilization (May 1–31 2026)...")
    utilization, utilization_url = fetch_utilization(credentials)
    talk = metric_by_email(utilization, "talk_time")
    hold = metric_by_email(utilization, "hold_time")
    util_inbound = metric_by_email(utilization, "inbound_calls")

    print("Fetching answered inbound calls day-by-day (May 1–31 2026)...")
    call_counts, transfer_counts, total_rows = raw_counts_from_calls(credentials, users)
    print(f"Total answered inbound rows fetched: {total_rows}")

    rows = []
    for agent in agents:
        email = agent["agent_email"]
        calls = util_inbound[email]["count"]
        transfers_all = transfer_counts[email]["all"]
        talk_seconds = int(round(talk[email]["total"]))
        hold_seconds = int(round(hold[email]["total"]))
        aht_seconds = round((talk_seconds + hold_seconds) / calls) if calls else 0

        rows.append({
            "agent_name": agent["agent_name"],
            "ctm_agent_name": "",
            "agent_email": email,
            "ctm_agent_id": agent["ctm_agent_id"],
            "inbound_answered_calls": calls,
            "transfers": transfers_all,
            "first_time_caller_transfers": transfer_counts[email]["first_time"],
            "repeat_caller_transfers": transfer_counts[email]["repeat"],
            "talk_time": hms(talk_seconds),
            "hold_time": hms(hold_seconds),
            "aht": hms(aht_seconds),
        })

    # --- Apply Google Sheets name mapping (Primary Key tab) ---
    mapped_count = 0
    mapping_label = "skipped (no Google credentials found)"
    google_creds = find_google_credentials_source()
    if google_creds:
        spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID", DEFAULT_SPREADSHEET_ID)
        agent_key_sheet = os.getenv("GOOGLE_AGENT_KEY_SHEET", DEFAULT_AGENT_KEY_SHEET)
        try:
            service = build_sheets_service(google_creds)
            key_values = get_sheet_values(service, spreadsheet_id, agent_key_sheet, "A:D")
            agent_name_map = build_agent_name_map(key_values)
            mapped_count = apply_agent_name_map(rows, agent_name_map)
            mapping_label = f"{mapped_count} agents remapped from '{agent_key_sheet}' ({google_creds['label']})"
            print(f"Name mapping: {mapping_label}")
        except HttpError as exc:
            detail = exc.content.decode("utf-8", errors="replace") if exc.content else str(exc)
            print(f"Warning: Google Sheets name mapping failed — {detail[:200]}")
    else:
        print(f"Name mapping: {mapping_label}")
        print("  Set GOOGLE_SERVICE_ACCOUNT_FILE (or JSON env vars) to enable.")

    fieldnames = [
        "agent_name",
        "ctm_agent_name",
        "agent_email",
        "ctm_agent_id",
        "inbound_answered_calls",
        "transfers",
        "first_time_caller_transfers",
        "repeat_caller_transfers",
        "talk_time",
        "hold_time",
        "aht",
    ]
    with REPORT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "date_range": {"start_date": START_DATE.isoformat(), "end_date": END_DATE.isoformat()},
        "timezone": {"name": TIMEZONE_NAME, "label": TIMEZONE_LABEL},
        "utilization_endpoint": utilization_url,
        "raw_inbound_call_rows_fetched": total_rows,
        "agent_count": len(rows),
        "name_mapping": mapping_label,
        "notes": [
            "inbound_answered_calls = /calls.json rows where direction=inbound AND status=answered, assigned to the agent",
            "transfers = answered inbound calls where the agent's SID appears in calls[].transfers[].from (first-time + repeat callers combined)",
            "talk_time and hold_time come from /utilization talk_time.total and hold_time.total",
            "aht = (talk_time + hold_time) / inbound_answered_calls",
            "ctm_agent_name = original name from CTM (only populated when name mapping was applied)",
            "forms excluded: CTM form submissions have direction=form and are not returned by direction=inbound",
        ],
        "columns": fieldnames,
        "rows": rows,
    }
    REPORT_JSON.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print_verification(rows, call_counts, transfer_counts, talk, hold, util_inbound)

    print()
    print(f"Wrote {REPORT_CSV}")
    print(f"Wrote {REPORT_JSON}")
    print(f"Utilization endpoint: {utilization_url}")


if __name__ == "__main__":
    main()
