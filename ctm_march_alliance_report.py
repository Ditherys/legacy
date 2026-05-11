import base64
import csv
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, time as dt_time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


START_DATE = date(2026, 3, 1)
END_DATE = date(2026, 3, 31)
EMAIL_DOMAIN = "@allianceglobalsolutions.com"
TIMEZONE_NAME = "America/New_York"
TIMEZONE_LABEL = "EST"
OUTPUT_DIR = Path("output")
CACHE_DIR = OUTPUT_DIR / "cache"
REPORT_CSV = OUTPUT_DIR / "ctm_alliance_march_2026_report.csv"
REPORT_JSON = OUTPUT_DIR / "ctm_alliance_march_2026_report.json"


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


def hms(seconds):
    total_seconds = int(round(float(seconds or 0)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


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
            "User-Agent": "legacy-ctm-alliance-march-report/1.0",
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
        rows.append(
            {
                "agent_name": name,
                "agent_email": email,
                "ctm_agent_id": (user.get("id") or "").strip(),
            }
        )
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
    payload, url = api_get(credentials, path, params)
    return payload, url


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
        print(f"{day.isoformat()} answered inbound page {page}: {len(calls)} rows")

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
    call_counts = defaultdict(lambda: {"answered": 0, "not_answered": 0})
    transfer_counts = defaultdict(lambda: {"all": 0, "first_time": 0, "repeat": 0})
    total_calls = 0

    for day in day_range(START_DATE, END_DATE):
        calls = fetch_day_answered_inbound_calls(credentials, day)
        total_calls += len(calls)
        for call in calls:
            assigned_email = ((call.get("agent") or {}).get("email") or "").strip().lower()
            is_answered = (call.get("status") or "").strip().lower() == "answered"
            if assigned_email.endswith(EMAIL_DOMAIN):
                if is_answered:
                    call_counts[assigned_email]["answered"] += 1
                else:
                    call_counts[assigned_email]["not_answered"] += 1

            if not is_answered:
                continue

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

    return call_counts, transfer_counts, total_calls


def main():
    load_dotenv(Path(".env.local"))
    OUTPUT_DIR.mkdir(exist_ok=True)

    credentials = get_credentials()
    users = fetch_all_users(credentials)
    agents = alliance_user_rows(users)
    print(f"Found {len(agents)} Alliance agents")

    utilization, utilization_url = fetch_utilization(credentials)
    talk = metric_by_email(utilization, "talk_time")
    hold = metric_by_email(utilization, "hold_time")

    call_counts, transfers, raw_inbound_call_rows = raw_counts_from_calls(credentials, users)

    rows = []
    for agent in agents:
        email = agent["agent_email"]
        answered = call_counts[email]["answered"]
        not_answered = call_counts[email]["not_answered"]
        talk_seconds = int(round(talk[email]["total"]))
        hold_seconds = int(round(hold[email]["total"]))
        aht_seconds = round((talk_seconds + hold_seconds) / answered) if answered else 0
        transfer = transfers[email]

        rows.append(
            {
                "agent_name": agent["agent_name"],
                "agent_email": email,
                "ctm_agent_id": agent["ctm_agent_id"],
                "inbound_answered_calls": answered,
                "inbound_not_answered_calls": not_answered,
                "talk_time": hms(talk_seconds),
                "hold_time": hms(hold_seconds),
                "aht": hms(aht_seconds),
                "transfer_including_repeat_callers": transfer["all"],
                "first_time_caller_transfer": transfer["first_time"],
                "repeat_caller_transfer": transfer["repeat"],
            }
        )

    fieldnames = [
        "agent_name",
        "agent_email",
        "ctm_agent_id",
        "inbound_answered_calls",
        "inbound_not_answered_calls",
        "talk_time",
        "hold_time",
        "aht",
        "transfer_including_repeat_callers",
        "first_time_caller_transfer",
        "repeat_caller_transfer",
    ]
    with REPORT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "date_range": {"start_date": START_DATE.isoformat(), "end_date": END_DATE.isoformat()},
        "timezone": {"name": TIMEZONE_NAME, "label": TIMEZONE_LABEL},
        "utilization_endpoint": utilization_url,
        "raw_inbound_call_rows_fetched": raw_inbound_call_rows,
        "agent_count": len(rows),
        "columns": fieldnames,
        "notes": [
            "inbound_answered_calls = raw /calls.json rows assigned to the agent where direction is inbound and status is answered",
            "inbound_not_answered_calls = raw /calls.json rows assigned to the agent where direction is inbound and status is not answered",
            "talk_time and hold_time come from CTM utilization talk_time.total and hold_time.total",
            "aht = (talk_time + hold_time) / inbound_answered_calls",
            "transfer columns are counted from raw inbound calls where status is answered and calls[].transfers[].from is the agent SID",
            "multiple transfer attempts by the same agent inside one call are counted as one transfer call",
        ],
        "rows": rows,
    }
    REPORT_JSON.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"Wrote {REPORT_CSV}")
    print(f"Wrote {REPORT_JSON}")
    print(f"Raw inbound call rows fetched: {raw_inbound_call_rows}")


if __name__ == "__main__":
    main()
