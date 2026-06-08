import argparse
import base64
import calendar
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


EMAIL_DOMAIN = "@allianceglobalsolutions.com"
TIMEZONE_NAME = "America/New_York"
TIMEZONE_LABEL = "EST"
LAST_UPDATED_TIMEZONE_NAME = "Asia/Manila"
OUTPUT_DIR = Path("output")
CACHE_DIR = OUTPUT_DIR / "kpi_cache"
HEADERS = [
    "Year",
    "Month",
    "Agent",
    "Attendance",
    "QA WK1 AVG",
    "QA WK2 AVG",
    "QA WK3 AVG",
    "QA WK4 AVG",
    "TTL AVG",
    "Calls",
    "Transfers",
    "Transfer Rate",
    "Transfers",
    "Admits",
    "Admission Rate",
    "Calls",
    "VOB",
    "VOB Rate",
    "Talk Time",
    "Hold Time",
    "Calls",
    "AHT",
    "Last Updated",
]


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


def month_number(value):
    if value.isdigit():
        number = int(value)
        if 1 <= number <= 12:
            return number
    normalized = value.strip().lower()
    for idx, month_name in enumerate(calendar.month_name):
        if month_name and month_name.lower() == normalized:
            return idx
    for idx, month_name in enumerate(calendar.month_abbr):
        if month_name and month_name.lower() == normalized:
            return idx
    raise SystemExit(f"Invalid month: {value}")


def parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"Invalid date '{value}'. Use YYYY-MM-DD.") from exc


def date_to_epoch(day, end_of_day=False):
    local_time = dt_time(23, 59, 59) if end_of_day else dt_time(0, 0, 0)
    local_dt = datetime.combine(day, local_time, tzinfo=ZoneInfo(TIMEZONE_NAME))
    return int(local_dt.timestamp())


def hms(seconds):
    total_seconds = int(round(float(seconds or 0)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def last_updated_timestamp():
    return datetime.now(ZoneInfo(LAST_UPDATED_TIMEZONE_NAME)).strftime("%Y-%m-%d %H:%M:%S")


def safe_rate(numerator, denominator):
    if not denominator:
        return ""
    return numerator / denominator


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
            "User-Agent": "legacy-ctm-kpi-raw-dry-run/1.0",
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
    users = []
    page = 1
    while True:
        payload, _url = api_get(credentials, path, {"per_page": 100, "page": page})
        batch = payload.get("users") or []
        if isinstance(batch, dict):
            batch = list(batch.values())
        users.extend(batch)
        if not payload.get("next_page"):
            return users
        page += 1


def alliance_agents(users):
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
                "agent": name,
                "email": email,
                "sid": (user.get("id") or "").strip(),
            }
        )
    return sorted(rows, key=lambda row: (row["agent"].lower(), row["email"]))


def fetch_utilization(credentials, start_date, end_date):
    path = f"/api/v1/accounts/{credentials['account_id']}/agents/utilization.json"
    params = {
        "start_time": date_to_epoch(start_date, end_of_day=False),
        "end_time": date_to_epoch(end_date, end_of_day=True),
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
    results = defaultdict(lambda: {"count": 0, "total": 0.0})
    for row in rows:
        user_id = str(row.get("user_id") or "").strip()
        email = ((users.get(user_id) or {}).get("email") or "").strip().lower()
        if not email:
            continue
        results[email]["count"] += int(float(row.get("count") or 0))
        results[email]["total"] += float(row.get("total") or 0)
    return results


def day_range(start_date, end_date):
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def fetch_day_inbound_calls(credentials, day, force_refresh=False):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"calls_inbound_{day.isoformat()}.json"
    if cache_path.exists() and not force_refresh:
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
        print(f"{day.isoformat()} inbound page {page}: {len(calls)} rows")

        after = payload.get("after")
        if not payload.get("next_page") or not after or not batch:
            break
        page += 1

    cache_path.write_text(json.dumps(calls), encoding="utf-8")
    return calls


def raw_counts(credentials, users, start_date, end_date, force_refresh=False, refresh_end_date=False):
    sid_to_email = {
        (user.get("id") or "").strip(): (user.get("email") or "").strip().lower()
        for user in users
        if (user.get("email") or "").strip().lower().endswith(EMAIL_DOMAIN)
    }
    counts = defaultdict(lambda: {"answered": 0, "not_answered": 0})
    transfers = defaultdict(lambda: {"all": 0, "first_time": 0, "repeat": 0})
    rows_fetched = 0

    for day in day_range(start_date, end_date):
        day_force_refresh = force_refresh or (refresh_end_date and day == end_date)
        calls = fetch_day_inbound_calls(credentials, day, force_refresh=day_force_refresh)
        rows_fetched += len(calls)
        for call in calls:
            assigned_email = ((call.get("agent") or {}).get("email") or "").strip().lower()
            is_answered = (call.get("status") or "").strip().lower() == "answered"
            if assigned_email.endswith(EMAIL_DOMAIN):
                if is_answered:
                    counts[assigned_email]["answered"] += 1
                else:
                    counts[assigned_email]["not_answered"] += 1

            if not is_answered:
                continue

            from_emails = set()
            for transfer in call.get("transfers") or []:
                from_email = sid_to_email.get((transfer.get("from") or "").strip())
                if from_email:
                    from_emails.add(from_email)

            for email in from_emails:
                transfers[email]["all"] += 1
                if call.get("is_new_caller"):
                    transfers[email]["first_time"] += 1
                else:
                    transfers[email]["repeat"] += 1

    return counts, transfers, rows_fetched


def build_rows(args, agents, utilization, call_counts, transfer_counts, run_timestamp):
    talk = metric_by_email(utilization, "talk_time")
    hold = metric_by_email(utilization, "hold_time")
    util_inbound = metric_by_email(utilization, "inbound_calls")
    outbound = metric_by_email(utilization, "outbound_calls")
    rows = []

    for agent in agents:
        email = agent["email"]
        calls = call_counts[email]["answered"]
        transfers = transfer_counts[email]["all"]
        talk_seconds = int(round(talk[email]["total"]))
        hold_seconds = int(round(hold[email]["total"]))
        outbound_calls = outbound[email]["count"]

        if args.active_only and not any([calls, transfers, talk_seconds, hold_seconds]):
            continue

        rows.append(
            {
                "Year": args.year,
                "Month": calendar.month_name[args.month],
                "Email": email,
                "Agent": agent["agent"],
                "Attendance": "",
                "QA WK1 AVG": "",
                "QA WK2 AVG": "",
                "QA WK3 AVG": "",
                "QA WK4 AVG": "",
                "TTL AVG": "",
                "Calls": calls,
                "Transfers": transfers,
                "Transfer Rate": "",
                "Transfers__admit_denominator": transfers,
                "Admits": "",
                "Admission Rate": "",
                "Calls__vob_denominator": calls,
                "VOB": "",
                "VOB Rate": "",
                "Talk Time": hms(talk_seconds),
                "Hold Time": hms(hold_seconds),
                "Calls__aht_denominator": calls,
                "AHT": "",
                "Last Updated": run_timestamp,
                "Outbound Calls": outbound_calls,
            }
        )

    return rows


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    csv_headers = [
        "Year",
        "Month",
        "Agent",
        "Attendance",
        "QA WK1 AVG",
        "QA WK2 AVG",
        "QA WK3 AVG",
        "QA WK4 AVG",
        "TTL AVG",
        "Calls",
        "Transfers",
        "Transfer Rate",
        "Transfers",
        "Admits",
        "Admission Rate",
        "Calls",
        "VOB",
        "VOB Rate",
        "Talk Time",
        "Hold Time",
        "Calls",
        "AHT",
        "Last Updated",
    ]
    internal_keys = [
        "Year",
        "Month",
        "Agent",
        "Attendance",
        "QA WK1 AVG",
        "QA WK2 AVG",
        "QA WK3 AVG",
        "QA WK4 AVG",
        "TTL AVG",
        "Calls",
        "Transfers",
        "Transfer Rate",
        "Transfers__admit_denominator",
        "Admits",
        "Admission Rate",
        "Calls__vob_denominator",
        "VOB",
        "VOB Rate",
        "Talk Time",
        "Hold Time",
        "Calls__aht_denominator",
        "AHT",
        "Last Updated",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(csv_headers)
        for row in rows:
            writer.writerow([row[key] for key in internal_keys])


def default_dates(year, month):
    start_date = date(year, month, 1)
    today_ctm = datetime.now(ZoneInfo(TIMEZONE_NAME)).date()
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    if today_ctm.year == year and today_ctm.month == month:
        end_date = min(today_ctm, last_day)
    else:
        end_date = last_day
    return start_date, end_date


def parse_args():
    today_ctm = datetime.now(ZoneInfo(TIMEZONE_NAME)).date()
    parser = argparse.ArgumentParser(description="Generate a KPI Raw-shaped CTM dry-run CSV.")
    parser.add_argument("--year", type=int, default=today_ctm.year)
    parser.add_argument("--month", type=month_number, default=today_ctm.month)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--output")
    parser.add_argument("--active-only", action="store_true", help="Only output agents with May CTM activity.")
    parser.add_argument("--force-refresh", action="store_true", help="Ignore cached daily calls and fetch fresh.")
    return parser.parse_args()


def main():
    load_dotenv(Path(".env.local"))
    args = parse_args()
    start_date, end_date = default_dates(args.year, args.month)
    if args.start_date:
        start_date = parse_date(args.start_date)
    if args.end_date:
        end_date = parse_date(args.end_date)
    if start_date > end_date:
        raise SystemExit("start-date must be on or before end-date.")

    if not args.output:
        month_slug = calendar.month_name[args.month].lower()
        args.output = str(OUTPUT_DIR / f"kpi_raw_{month_slug}_{args.year}_dry_run.csv")

    credentials = get_credentials()
    users = fetch_all_users(credentials)
    agents = alliance_agents(users)
    utilization, utilization_url = fetch_utilization(credentials, start_date, end_date)
    call_counts, transfer_counts, rows_fetched = raw_counts(
        credentials,
        users,
        start_date,
        end_date,
        force_refresh=args.force_refresh,
    )
    run_timestamp = last_updated_timestamp()
    rows = build_rows(args, agents, utilization, call_counts, transfer_counts, run_timestamp)
    output_path = Path(args.output)
    write_csv(output_path, rows)

    summary = {
        "year": args.year,
        "month": calendar.month_name[args.month],
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "timezone": {"name": TIMEZONE_NAME, "label": TIMEZONE_LABEL},
        "agent_rows": len(rows),
        "raw_inbound_call_rows_fetched": rows_fetched,
        "utilization_endpoint": utilization_url,
        "output": str(output_path),
    }
    summary_path = output_path.with_suffix(".json")
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"Wrote {output_path}")
    print(f"Wrote {summary_path}")
    print(f"Agent rows: {len(rows)}")
    print(f"Date range: {start_date} to {end_date}")
    print(f"Raw inbound call rows fetched: {rows_fetched}")


if __name__ == "__main__":
    main()
