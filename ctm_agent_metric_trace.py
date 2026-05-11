import argparse
import base64
import csv
import json
import os
import re
from collections import Counter
from datetime import datetime, time as dt_time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


DEFAULT_AGENT_EMAIL = "r.sagun@allianceglobalsolutions.com"
DEFAULT_TIMEZONE_NAME = "America/New_York"
DEFAULT_TIMEZONE_LABEL = "EST"
OUTPUT_DIR = Path("output")


def load_dotenv(path):
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def get_first_env(*names, required=True, default=None):
    for name in names:
        value = os.getenv(name)
        if value:
            return value

    if required:
        joined = " or ".join(names)
        raise SystemExit(f"Missing required environment variable: {joined}")

    return default


def get_credentials():
    return {
        "api_host": get_first_env("CTM_API_HOST", required=False, default="https://api.calltrackingmetrics.com").rstrip("/"),
        "access_key": get_first_env("CTM_ACCESS_KEY", "CTM_API_KEY"),
        "secret_key": get_first_env("CTM_SECRET_KEY", "CTM_API_SECRET"),
        "account_id": get_first_env("CTM_ACCOUNT_ID"),
    }


def parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"Invalid date '{value}'. Use YYYY-MM-DD.") from exc


def date_to_epoch(date_string, timezone_name, end_of_day):
    day = parse_date(date_string)
    local_time = dt_time(23, 59, 59) if end_of_day else dt_time(0, 0, 0)
    local_dt = datetime.combine(day, local_time, tzinfo=ZoneInfo(timezone_name))
    return int(local_dt.timestamp())


def seconds_to_hms(value):
    total_seconds = int(round(float(value or 0)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def hms_to_seconds(value):
    if value is None:
        return None

    pieces = str(value).strip().split(":")
    if len(pieces) != 3:
        raise SystemExit(f"Invalid time '{value}'. Use H:MM:SS.")

    hours, minutes, seconds = (int(piece) for piece in pieces)
    return hours * 3600 + minutes * 60 + seconds


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
            "User-Agent": "legacy-ctm-agent-metric-trace/1.0",
        },
    )

    try:
        with urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8")
            return json.loads(body), url
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
        payload, url = api_get(credentials, path, {"per_page": 100, "page": page})
        rows = payload.get("users") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        users.extend(rows)

        if not payload.get("next_page"):
            return users, url

        page += 1


def fetch_utilization(credentials, args):
    path = f"/api/v1/accounts/{credentials['account_id']}/agents/utilization.json"
    params = {
        "start_time": date_to_epoch(args.start_date, args.timezone_name, end_of_day=False),
        "end_time": date_to_epoch(args.end_date, args.timezone_name, end_of_day=True),
        "timezone": args.timezone_label,
        "interval": args.interval,
        "statistic": args.statistic,
        "view_by": "agent",
        "team_id": args.team_id,
        "user_ids": args.user_ids,
        "queue_ids": args.queue_ids,
        "user_group_id": args.user_group_id,
        "es": args.es,
    }
    return api_get(credentials, path, params)


def fetch_agent_events(credentials, args, user_id):
    path = f"/api/v1/accounts/{credentials['account_id']}/agents/events.json"
    params = {
        "user_id": user_id,
        "start_time": date_to_epoch(args.start_date, args.timezone_name, end_of_day=False),
        "end_time": date_to_epoch(args.end_date, args.timezone_name, end_of_day=True),
    }
    return api_get(credentials, path, params)


def fetch_calls(credentials, args, direction):
    path = f"/api/v1/accounts/{credentials['account_id']}/calls.json"
    calls = []
    cursor = None
    final_url = None

    while True:
        params = {
            "per_page": 50,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "direction": direction,
            "status": args.calls_status,
            "filter": args.calls_filter,
        }
        if cursor:
            params["after"] = cursor

        payload, final_url = api_get(credentials, path, params)
        batch = payload.get("calls") or []
        calls.extend(batch)

        cursor = payload.get("after")
        if not payload.get("next_page") or not cursor or not batch:
            return calls, final_url


def summarize_raw_calls(calls, target_email, target_user_sid):
    filtered = [
        call for call in calls
        if (((call.get("agent") or {}).get("email") or "").strip().lower() == target_email)
    ]
    transfer_from_calls = []
    transfer_from_completed_calls = []
    transfer_from_events = []
    transfer_from_completed_events = []

    for call in calls:
        matched_transfer = False
        matched_completed_transfer = False
        for transfer in call.get("transfers") or []:
            from_id = (transfer.get("from") or "").strip()
            if from_id != target_user_sid:
                continue

            transfer_from_events.append(transfer)
            matched_transfer = True
            if (transfer.get("status") or "").strip().lower() == "completed":
                transfer_from_completed_events.append(transfer)
                matched_completed_transfer = True

        if matched_transfer:
            transfer_from_calls.append(call)
        if matched_completed_transfer:
            transfer_from_completed_calls.append(call)

    return {
        "account_call_rows_fetched": len(calls),
        "agent_call_rows": len(filtered),
        "assigned_agent_calls_with_transfers": sum(1 for call in filtered if call.get("transfers")),
        "transfer_from_agent_call_count": len(transfer_from_calls),
        "transfer_from_agent_first_time_call_count": sum(1 for call in transfer_from_calls if call.get("is_new_caller")),
        "transfer_from_agent_repeat_call_count": sum(1 for call in transfer_from_calls if not call.get("is_new_caller")),
        "transfer_from_agent_event_count": len(transfer_from_events),
        "transfer_from_agent_completed_call_count": len(transfer_from_completed_calls),
        "transfer_from_agent_completed_first_time_call_count": sum(
            1 for call in transfer_from_completed_calls if call.get("is_new_caller")
        ),
        "transfer_from_agent_completed_repeat_call_count": sum(
            1 for call in transfer_from_completed_calls if not call.get("is_new_caller")
        ),
        "transfer_from_agent_completed_event_count": len(transfer_from_completed_events),
        "status_counts": dict(Counter((call.get("status") or "").strip() or "(blank)" for call in filtered)),
        "dial_status_counts": dict(Counter((call.get("dial_status") or "").strip() or "(blank)" for call in filtered)),
        "call_status_counts": dict(Counter((call.get("call_status") or "").strip() or "(blank)" for call in filtered)),
        "direction_counts": dict(Counter((call.get("direction") or "").strip() or "(blank)" for call in filtered)),
        "talk_time_seconds": int(round(sum(float(call.get("talk_time") or 0) for call in filtered))),
        "talk_time_hms": seconds_to_hms(sum(float(call.get("talk_time") or 0) for call in filtered)),
        "hold_time_seconds": int(round(sum(float(call.get("hold_time") or 0) for call in filtered))),
        "hold_time_hms": seconds_to_hms(sum(float(call.get("hold_time") or 0) for call in filtered)),
        "transfer_from_agent_call_samples": [
            {
                "id": call.get("id"),
                "called_at": call.get("called_at"),
                "status": call.get("status"),
                "dial_status": call.get("dial_status"),
                "call_status": call.get("call_status"),
                "is_new_caller": call.get("is_new_caller"),
                "assigned_agent_email": ((call.get("agent") or {}).get("email") or ""),
                "transfers_from_agent": [
                    {
                        key: transfer.get(key)
                        for key in sorted(transfer.keys())
                    }
                    for transfer in call.get("transfers") or []
                    if (transfer.get("from") or "").strip() == target_user_sid
                ],
            }
            for call in transfer_from_calls[:10]
        ],
    }


def summarize_agent_events(payload):
    events = payload.get("events") or []
    by_event = {}
    for event in events:
        name = event.get("event") or "(blank)"
        current = by_event.setdefault(name, {"count": 0, "total_seconds": 0})
        current["count"] += int(float(event.get("count") or 0))
        current["total_seconds"] += int(round(float(event.get("total") or 0)))

    for value in by_event.values():
        value["total_hms"] = seconds_to_hms(value["total_seconds"])

    return {
        "event_rows": len(events),
        "events": dict(sorted(by_event.items())),
    }


def normalize_users(payload_users):
    if isinstance(payload_users, dict):
        return {str(key): value for key, value in payload_users.items()}

    if isinstance(payload_users, list):
        return {str(user.get("id")): user for user in payload_users if user.get("id")}

    return {}


def build_metric_map(payload, metric_name):
    users = normalize_users(payload.get("users") or {})
    rows = (payload.get("metrics") or {}).get(metric_name) or []
    results = {}

    for row in rows:
        user_id = str(row.get("user_id") or "").strip()
        user = users.get(user_id) or {}
        email = (user.get("email") or "").strip().lower()
        if not email:
            continue

        if email not in results:
            results[email] = dict(row)
            continue

        existing = results[email]
        for field in ("count", "total"):
            existing[field] = float(existing.get(field) or 0) + float(row.get(field) or 0)

    return results


def metric_total(payload, metric_name, email):
    row = build_metric_map(payload, metric_name).get(email) or {}
    return {
        "count": int(float(row.get("count") or 0)),
        "total_seconds": int(round(float(row.get("total") or 0))),
        "total_hms": seconds_to_hms(row.get("total") or 0),
        "raw": row,
    }


def metric_breakdown(payload, metric_name, email):
    users = normalize_users(payload.get("users") or {})
    rows = (payload.get("metrics") or {}).get(metric_name) or []
    target = email.strip().lower()
    breakdown = []

    for row in rows:
        user_id = str(row.get("user_id") or "").strip()
        user = users.get(user_id) or {}
        user_email = (user.get("email") or "").strip().lower()
        if user_email != target:
            continue

        breakdown.append(
            {
                "metric": metric_name,
                "user_id": user_id,
                "name": user.get("name"),
                "email": user.get("email"),
                "count": int(float(row.get("count") or 0)),
                "total_seconds": int(round(float(row.get("total") or 0))),
                "total_hms": seconds_to_hms(row.get("total") or 0),
            }
        )

    return breakdown


def find_user(users, target_email):
    target = target_email.lower()
    for user in users:
        if (user.get("email") or "").strip().lower() == target:
            return {
                "id": user.get("id"),
                "name": user.get("name") or " ".join(
                    piece for piece in [user.get("first_name"), user.get("last_name")] if piece
                ).strip(),
                "email": user.get("email"),
            }
    return None


def choose_calls_metric(metrics, expected_calls):
    candidates = [
        "talk_time",
        "handle_time",
        "inbound_calls",
        "offered_calls",
        "outbound_calls",
        "internal_calls",
    ]
    if expected_calls is None:
        return "talk_time"

    for metric_name in candidates:
        if metrics[metric_name]["count"] == expected_calls:
            return metric_name

    return "talk_time"


def ordered_metric_names(payload):
    preferred = [
        "talk_time",
        "handle_time",
        "inbound_calls",
        "outbound_calls",
        "internal_calls",
        "offered_calls",
        "hold_time",
        "wrapup_time",
        "missed_dials",
        "ignored",
        "ignored_reload",
    ]
    available = list((payload.get("metrics") or {}).keys())
    ordered = [name for name in preferred if name in available]
    ordered.extend(sorted(name for name in available if name not in set(ordered)))
    return ordered


def write_summary(summary, output_prefix):
    OUTPUT_DIR.mkdir(exist_ok=True)
    json_path = OUTPUT_DIR / f"{output_prefix}.json"
    csv_path = OUTPUT_DIR / f"{output_prefix}.csv"

    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    row = {
        "agent_name": summary["agent"].get("name"),
        "agent_email": summary["agent"].get("email"),
        "start_date": summary["date_range"]["start_date"],
        "end_date": summary["date_range"]["end_date"],
        "calls_metric_used_for_aht": summary["computed"]["calls_metric_used_for_aht"],
        "calls": summary["computed"]["calls"],
        "talk_time": summary["computed"]["talk_time_hms"],
        "hold_time": summary["computed"]["hold_time_hms"],
        "aht": summary["computed"]["aht_hms"],
        "utilization_endpoint": summary["endpoints"]["utilization"],
    }
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        writer.writeheader()
        writer.writerow(row)

    return json_path, csv_path


def slugify(value):
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower())
    return slug.strip("_") or "agent"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Trace Rae Sagun's CTM Agent Activity totals back to the API endpoint."
    )
    parser.add_argument("start_date", help="Rawfile start date, YYYY-MM-DD")
    parser.add_argument("end_date", help="Rawfile end date, YYYY-MM-DD")
    parser.add_argument("--agent-email", default=DEFAULT_AGENT_EMAIL)
    parser.add_argument("--team-id", default="")
    parser.add_argument("--user-ids", default="")
    parser.add_argument("--queue-ids", default="")
    parser.add_argument("--user-group-id", default="")
    parser.add_argument("--timezone-name", default=DEFAULT_TIMEZONE_NAME)
    parser.add_argument("--timezone-label", default=DEFAULT_TIMEZONE_LABEL)
    parser.add_argument("--interval", default="day")
    parser.add_argument("--statistic", default="occupancy")
    parser.add_argument("--es", default="1")
    parser.add_argument("--expected-calls", type=int)
    parser.add_argument("--expected-talk-time")
    parser.add_argument("--expected-hold-time")
    parser.add_argument("--audit-calls", action="store_true", help="Also fetch /calls.json inbound rows and count raw records for the agent.")
    parser.add_argument("--calls-filter", default="", help="Optional CTM calls filter, for example: agent.name:\"Rae Sagun\"")
    parser.add_argument("--calls-direction", default="inbound")
    parser.add_argument("--calls-status", default="")
    parser.add_argument("--audit-events", action="store_true", help="Also fetch /agents/events.json for the matched internal CTM user id.")
    return parser.parse_args()


def main():
    load_dotenv(Path(".env.local"))
    args = parse_args()
    if parse_date(args.start_date) > parse_date(args.end_date):
        raise SystemExit("start_date must be on or before end_date.")

    credentials = get_credentials()
    target_email = args.agent_email.strip().lower()

    users, users_url = fetch_all_users(credentials)
    agent = find_user(users, target_email)
    if not agent:
        raise SystemExit(f"Could not find agent email in CTM users endpoint: {target_email}")

    payload, utilization_url = fetch_utilization(credentials, args)
    metrics = {name: metric_total(payload, name, target_email) for name in ordered_metric_names(payload)}

    calls_metric = choose_calls_metric(metrics, args.expected_calls)
    calls = metrics[calls_metric]["count"]
    talk_seconds = metrics["talk_time"]["total_seconds"]
    hold_seconds = metrics["hold_time"]["total_seconds"]
    aht_seconds = round((talk_seconds + hold_seconds) / calls) if calls else 0

    expected = {
        "calls": args.expected_calls,
        "talk_time_seconds": hms_to_seconds(args.expected_talk_time),
        "hold_time_seconds": hms_to_seconds(args.expected_hold_time),
    }
    expected["aht_seconds"] = (
        round((expected["talk_time_seconds"] + expected["hold_time_seconds"]) / expected["calls"])
        if all(expected.values())
        else None
    )

    comparison = {
        "calls_match": expected["calls"] is None or calls == expected["calls"],
        "talk_time_match": expected["talk_time_seconds"] is None or talk_seconds == expected["talk_time_seconds"],
        "hold_time_match": expected["hold_time_seconds"] is None or hold_seconds == expected["hold_time_seconds"],
        "aht_match": expected["aht_seconds"] is None or aht_seconds == expected["aht_seconds"],
    }

    summary = {
        "agent": agent,
        "date_range": {
            "start_date": args.start_date,
            "end_date": args.end_date,
            "timezone_name": args.timezone_name,
            "timezone_label": args.timezone_label,
        },
        "endpoints": {
            "users": users_url,
            "utilization": utilization_url,
        },
        "metrics": {
            name: {
                "count": value["count"],
                "total_seconds": value["total_seconds"],
                "total_hms": value["total_hms"],
            }
            for name, value in metrics.items()
        },
        "metric_breakdown": {
            name: metric_breakdown(payload, name, target_email)
            for name in ["inbound_calls", "outbound_calls", "talk_time", "hold_time", "offered_calls"]
        },
        "computed": {
            "calls_metric_used_for_aht": calls_metric,
            "calls": calls,
            "talk_time_seconds": talk_seconds,
            "talk_time_hms": seconds_to_hms(talk_seconds),
            "hold_time_seconds": hold_seconds,
            "hold_time_hms": seconds_to_hms(hold_seconds),
            "aht_seconds": aht_seconds,
            "aht_hms": seconds_to_hms(aht_seconds),
            "formula": "(talk_time.total + hold_time.total) / calls_metric.count",
            "note": "If the rawfile column is 'Inbound calls', compare against inbound_calls.count instead.",
        },
        "expected": {
            "calls": expected["calls"],
            "talk_time_hms": seconds_to_hms(expected["talk_time_seconds"]) if expected["talk_time_seconds"] is not None else None,
            "hold_time_hms": seconds_to_hms(expected["hold_time_seconds"]) if expected["hold_time_seconds"] is not None else None,
            "aht_hms": seconds_to_hms(expected["aht_seconds"]) if expected["aht_seconds"] is not None else None,
        },
        "comparison": comparison,
        "metrics_matching_expected_calls": [
            name for name, value in metrics.items() if args.expected_calls is not None and value["count"] == args.expected_calls
        ],
    }

    event_user_id = None
    if summary["metric_breakdown"]["talk_time"]:
        event_user_id = summary["metric_breakdown"]["talk_time"][0]["user_id"]

    if args.audit_events and event_user_id:
        event_payload, events_url = fetch_agent_events(credentials, args, event_user_id)
        summary["endpoints"]["events"] = events_url
        summary["agent_events_audit"] = summarize_agent_events(event_payload)

    if args.audit_calls:
        raw_calls, raw_calls_url = fetch_calls(credentials, args, direction=args.calls_direction)
        summary["endpoints"]["calls"] = raw_calls_url
        summary["raw_calls_audit"] = summarize_raw_calls(raw_calls, target_email, agent.get("id") or "")

    prefix = f"{slugify(agent.get('name') or target_email)}_ctm_trace_{args.start_date}_to_{args.end_date}"
    json_path, csv_path = write_summary(summary, prefix)

    print("CTM Agent Activity endpoint trace")
    print(f"Agent: {agent.get('name')} <{agent.get('email')}>")
    print(f"Users endpoint: {users_url}")
    print(f"Utilization endpoint: {utilization_url}")
    print(f"Calls metric used for AHT: {calls_metric}")
    print(f"Calls: {calls}")
    print(f"Talk Time: {seconds_to_hms(talk_seconds)}")
    print(f"Hold Time: {seconds_to_hms(hold_seconds)}")
    print(f"AHT: {seconds_to_hms(aht_seconds)}")
    if args.audit_calls:
        raw_audit = summary["raw_calls_audit"]
        print(f"Calls endpoint: {summary['endpoints']['calls']}")
        print(f"Raw inbound call rows for agent: {raw_audit['agent_call_rows']}")
        print(f"Raw inbound talk time: {raw_audit['talk_time_hms']}")
        print(f"Raw inbound hold time: {raw_audit['hold_time_hms']}")
        print(f"Transfer-from-agent calls: {raw_audit['transfer_from_agent_call_count']}")
        print(f"Transfer-from-agent first-time calls: {raw_audit['transfer_from_agent_first_time_call_count']}")
        print(f"Transfer-from-agent repeat calls: {raw_audit['transfer_from_agent_repeat_call_count']}")
        print(f"Completed transfer-from-agent calls: {raw_audit['transfer_from_agent_completed_call_count']}")
    if args.audit_events and "agent_events_audit" in summary:
        print(f"Agent events endpoint: {summary['endpoints']['events']}")
        for event_name in ["inbound", "outbound", "start_hold_party", "answered"]:
            event = summary["agent_events_audit"]["events"].get(event_name)
            if event:
                print(f"Event {event_name}: count={event['count']}, total={event['total_hms']}")
    print(f"Wrote: {json_path}")
    print(f"Wrote: {csv_path}")

    if args.expected_calls or args.expected_talk_time or args.expected_hold_time:
        print("Comparison:")
        for key, matched in comparison.items():
            print(f"  {key}: {'MATCH' if matched else 'DIFFERENT'}")


if __name__ == "__main__":
    main()
