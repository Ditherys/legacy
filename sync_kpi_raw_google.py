import argparse
import base64
import calendar
import json
import os
import re
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import ctm_kpi_raw_dry_run as ctm


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DEFAULT_SPREADSHEET_ID = "1vMJwoOoFC9jg0mOAOwT2i1iWSVNh16PsQ89U2Icr2AU"
DEFAULT_SHEET_NAME = "KPI Raw"
DEFAULT_AGENT_KEY_SHEET = "Primary Key"
DEFAULT_CREDENTIALS_GLOB = "secrets/*.json"

ROW_KEYS = [
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


def load_dotenv():
    ctm.load_dotenv(Path(".env.local"))


def parse_spreadsheet_id(value):
    if not value:
        return DEFAULT_SPREADSHEET_ID
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", value)
    return match.group(1) if match else value


def find_credentials_file(value):
    if value:
        path = Path(value)
        if not path.exists():
            raise SystemExit(f"Google credentials file does not exist: {path}")
        return path

    matches = sorted(Path(".").glob(DEFAULT_CREDENTIALS_GLOB))
    if not matches:
        raise SystemExit(
            "Missing Google credentials JSON. Set GOOGLE_SERVICE_ACCOUNT_FILE "
            f"or place one file under {DEFAULT_CREDENTIALS_GLOB}."
        )
    if len(matches) > 1:
        raise SystemExit(
            "More than one Google credentials JSON found. Set GOOGLE_SERVICE_ACCOUNT_FILE "
            "to choose one explicitly."
        )
    return matches[0]


def find_credentials_source(value):
    json_b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
    if json_b64:
        try:
            decoded = base64.b64decode(json_b64).decode("utf-8")
            return {
                "type": "json",
                "label": "GOOGLE_SERVICE_ACCOUNT_JSON_B64",
                "value": json.loads(decoded),
            }
        except (ValueError, json.JSONDecodeError) as exc:
            raise SystemExit("GOOGLE_SERVICE_ACCOUNT_JSON_B64 is not valid base64-encoded JSON.") from exc

    json_text = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if json_text:
        try:
            return {
                "type": "json",
                "label": "GOOGLE_SERVICE_ACCOUNT_JSON",
                "value": json.loads(json_text),
            }
        except json.JSONDecodeError as exc:
            raise SystemExit("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON.") from exc

    path = find_credentials_file(value)
    return {"type": "file", "label": str(path), "value": str(path)}


def month_number(value):
    return ctm.month_number(str(value))


def normalize_value(value):
    return " ".join(str(value or "").strip().lower().split())


def normalize_header(value):
    return re.sub(r"[^a-z0-9]", "", normalize_value(value))


def row_key(year, month, agent):
    return (str(year).strip(), normalize_value(month), normalize_value(agent))


def sheet_range(sheet_name, a1_range):
    escaped_name = sheet_name.replace("'", "''")
    return f"'{escaped_name}'!{a1_range}"


def build_sheets_service(credentials_source):
    if isinstance(credentials_source, dict) and credentials_source.get("type") == "json":
        credentials = service_account.Credentials.from_service_account_info(
            credentials_source["value"],
            scopes=SCOPES,
        )
    else:
        credentials_file = credentials_source["value"] if isinstance(credentials_source, dict) else credentials_source
        credentials = service_account.Credentials.from_service_account_file(
            credentials_file,
            scopes=SCOPES,
        )
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def get_sheet_values(service, spreadsheet_id, sheet_name, a1_range="A:W"):
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=sheet_range(sheet_name, a1_range))
        .execute()
    )
    return result.get("values", [])


def get_optional_sheet_values(service, spreadsheet_id, sheet_name, a1_range):
    if not sheet_name:
        return []
    try:
        return get_sheet_values(service, spreadsheet_id, sheet_name, a1_range)
    except HttpError as exc:
        if exc.resp.status in (400, 404):
            return []
        raise


def row_values(row):
    return [row.get(key, "") for key in ROW_KEYS]


def pad(values, length):
    if len(values) >= length:
        return values
    return values + [""] * (length - len(values))


def existing_row_map(values):
    rows = {}
    duplicates = []
    for index, raw_row in enumerate(values[1:], start=2):
        padded = pad(raw_row, 3)
        if not any(str(cell).strip() for cell in padded[:3]):
            continue
        key = row_key(padded[0], padded[1], padded[2])
        if key in rows:
            duplicates.append(index)
            continue
        rows[key] = index
    return rows, duplicates


def column_index(headers, aliases):
    normalized = [normalize_header(header) for header in headers]
    for alias in aliases:
        try:
            return normalized.index(normalize_header(alias))
        except ValueError:
            continue
    return None


def build_agent_name_map(values):
    if not values:
        return {"by_email": {}, "by_name": {}, "rows": 0, "available": False}

    headers = values[0]
    email_index = column_index(headers, ["CTM Email", "Email", "ctm_email"])
    ctm_name_index = column_index(headers, ["CTM_Agent", "CTM Agent", "CTM Agent Name", "CTM Name", "ctm_agent_name"])
    kpi_name_index = column_index(headers, ["KPI Agent Name", "KPI Name", "Agent", "Agents", "kpi_agent_name"])

    if kpi_name_index is None:
        raise SystemExit(
            "Agent key sheet needs a KPI Agent Name column. "
            "Recommended headers: CTM Email, CTM Agent Name, KPI Agent Name."
        )

    by_email = {}
    by_name = {}
    rows = 0
    for raw_row in values[1:]:
        row = pad(raw_row, len(headers))
        kpi_name = row[kpi_name_index].strip() if row[kpi_name_index:] else ""
        if not kpi_name:
            continue

        if email_index is not None and email_index < len(row):
            email = normalize_value(row[email_index])
            if email:
                by_email[email] = kpi_name

        if ctm_name_index is not None and ctm_name_index < len(row):
            ctm_name = normalize_value(row[ctm_name_index])
            if ctm_name:
                by_name[ctm_name] = kpi_name

        rows += 1

    return {"by_email": by_email, "by_name": by_name, "rows": rows, "available": True}


def apply_agent_name_map(rows, agent_name_map):
    mapped = 0
    for row in rows:
        original_agent = row["Agent"]
        mapped_agent = agent_name_map["by_email"].get(normalize_value(row.get("Email")))
        if not mapped_agent:
            mapped_agent = agent_name_map["by_name"].get(normalize_value(original_agent))
        if mapped_agent and mapped_agent != original_agent:
            row["CTM Agent"] = original_agent
            row["Agent"] = mapped_agent
            mapped += 1
    return mapped


def managed_cell_updates(sheet_name, row_number, values):
    return [
        {
            "range": sheet_range(sheet_name, f"J{row_number}:K{row_number}"),
            "values": [[values[9], values[10]]],
        },
        {
            "range": sheet_range(sheet_name, f"M{row_number}:M{row_number}"),
            "values": [[values[12]]],
        },
        {
            "range": sheet_range(sheet_name, f"P{row_number}:P{row_number}"),
            "values": [[values[15]]],
        },
        {
            "range": sheet_range(sheet_name, f"S{row_number}:U{row_number}"),
            "values": [[values[18], values[19], values[20]]],
        },
        {
            "range": sheet_range(sheet_name, f"W{row_number}:W{row_number}"),
            "values": [[values[22]]],
        },
    ]


def generate_kpi_rows(args):
    start_date, end_date = ctm.default_dates(args.year, args.month)
    if args.start_date:
        start_date = ctm.parse_date(args.start_date)
    if args.end_date:
        end_date = ctm.parse_date(args.end_date)
    if start_date > end_date:
        raise SystemExit("start-date must be on or before end-date.")

    ctm_args = argparse.Namespace(
        year=args.year,
        month=args.month,
        active_only=args.active_only,
    )
    credentials = ctm.get_credentials()
    users = ctm.fetch_all_users(credentials)
    agents = ctm.alliance_agents(users)
    utilization, utilization_url = ctm.fetch_utilization(credentials, start_date, end_date)
    call_counts, transfer_counts, rows_fetched = ctm.raw_counts(
        credentials,
        users,
        start_date,
        end_date,
        force_refresh=args.force_refresh,
        refresh_end_date=getattr(args, "refresh_end_date", False),
    )
    run_timestamp = ctm.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = ctm.build_rows(
        ctm_args,
        agents,
        utilization,
        call_counts,
        transfer_counts,
        run_timestamp,
    )
    return rows, {
        "year": args.year,
        "month": calendar.month_name[args.month],
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "timezone": {"name": ctm.TIMEZONE_NAME, "label": ctm.TIMEZONE_LABEL},
        "agent_rows": len(rows),
        "raw_inbound_call_rows_fetched": rows_fetched,
        "utilization_endpoint": utilization_url,
    }


def write_summary(path, summary):
    if not path:
        return
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def sync_rows(service, spreadsheet_id, sheet_name, rows, dry_run=False, clear_aht_column=False):
    values = get_sheet_values(service, spreadsheet_id, sheet_name)
    if not values:
        raise SystemExit(f"Sheet tab '{sheet_name}' is empty or missing headers.")

    headers = pad(values[0], len(ctm.HEADERS))
    expected = ctm.HEADERS
    if headers[: len(expected)] != expected:
        raise SystemExit(
            "KPI Raw headers do not match expected A:W layout. "
            "Please confirm the target tab and column order."
        )

    existing, duplicates = existing_row_map(values)
    updates = []
    appends = []
    clear_ranges = []
    updated_agents = []
    appended_agents = []

    for row in rows:
        values_for_row = row_values(row)
        key = row_key(row["Year"], row["Month"], row["Agent"])
        original_agent = row.get("CTM Agent") or row["Agent"]
        original_key = row_key(row["Year"], row["Month"], original_agent)
        row_number = existing.get(key)
        rename_existing_agent = False
        if not row_number and original_key != key:
            row_number = existing.get(original_key)
            rename_existing_agent = bool(row_number)
        if row_number:
            if rename_existing_agent:
                updates.append(
                    {
                        "range": sheet_range(sheet_name, f"C{row_number}:C{row_number}"),
                        "values": [[row["Agent"]]],
                    }
                )
            updates.extend(managed_cell_updates(sheet_name, row_number, values_for_row))
            if clear_aht_column:
                clear_ranges.append(sheet_range(sheet_name, f"V{row_number}:V{row_number}"))
            updated_agents.append(row["Agent"])
        else:
            appends.append(values_for_row)
            appended_agents.append(row["Agent"])

    if not dry_run:
        if clear_ranges:
            (
                service.spreadsheets()
                .values()
                .batchClear(
                    spreadsheetId=spreadsheet_id,
                    body={"ranges": clear_ranges},
                )
                .execute()
            )
        if updates:
            (
                service.spreadsheets()
                .values()
                .batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={
                        "valueInputOption": "USER_ENTERED",
                        "data": updates,
                    },
                )
                .execute()
            )
        if appends:
            (
                service.spreadsheets()
                .values()
                .append(
                    spreadsheetId=spreadsheet_id,
                    range=sheet_range(sheet_name, "A:W"),
                    valueInputOption="USER_ENTERED",
                    insertDataOption="INSERT_ROWS",
                    body={"values": appends},
                )
                .execute()
            )

    return {
        "updated_rows": len(updated_agents),
        "appended_rows": len(appended_agents),
        "updated_agents": updated_agents,
        "appended_agents": appended_agents,
        "duplicate_existing_rows_skipped": duplicates,
        "aht_cells_cleared": len(clear_ranges),
        "dry_run": dry_run,
    }


def parse_args(argv=None):
    ctm_today = ctm.datetime.now(ctm.ZoneInfo(ctm.TIMEZONE_NAME)).date()
    parser = argparse.ArgumentParser(description="Sync CTM KPI Raw rows to Google Sheets.")
    parser.add_argument("--spreadsheet-id", default=os.getenv("GOOGLE_SPREADSHEET_ID", DEFAULT_SPREADSHEET_ID))
    parser.add_argument("--sheet", default=os.getenv("GOOGLE_KPI_RAW_SHEET", DEFAULT_SHEET_NAME))
    parser.add_argument("--agent-key-sheet", default=os.getenv("GOOGLE_AGENT_KEY_SHEET", DEFAULT_AGENT_KEY_SHEET))
    parser.add_argument("--credentials", default=os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE"))
    parser.add_argument("--year", type=int, default=ctm_today.year)
    parser.add_argument("--month", type=month_number, default=ctm_today.month)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--active-only", action="store_true")
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--refresh-end-date", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--show-matching-rows", action="store_true")
    parser.add_argument(
        "--clear-aht-column",
        action="store_true",
        help="Clear existing AHT cells in column V for matching rows so the sheet formula can own them.",
    )
    parser.add_argument("--summary-output", default=str(ctm.OUTPUT_DIR / "kpi_google_sync_summary.json"))
    return parser.parse_args(argv)


def main(argv=None):
    load_dotenv()
    args = parse_args(argv)
    args.spreadsheet_id = parse_spreadsheet_id(args.spreadsheet_id)
    credentials_source = find_credentials_source(args.credentials)

    try:
        service = build_sheets_service(credentials_source)
        rows, summary = generate_kpi_rows(args)
        agent_key_values = get_optional_sheet_values(
            service,
            args.spreadsheet_id,
            args.agent_key_sheet,
            "A:D",
        )
        agent_name_map = build_agent_name_map(agent_key_values)
        mapped_agent_rows = apply_agent_name_map(rows, agent_name_map)
        sync_summary = sync_rows(
            service,
            args.spreadsheet_id,
            args.sheet,
            rows,
            dry_run=args.dry_run,
            clear_aht_column=args.clear_aht_column,
        )
        if args.show_matching_rows:
            values = get_sheet_values(service, args.spreadsheet_id, args.sheet)
            target_keys = {row_key(row["Year"], row["Month"], row["Agent"]) for row in rows}
            print("Matching Google Sheet rows:")
            for index, raw_row in enumerate(values[1:], start=2):
                padded = pad(raw_row, len(ctm.HEADERS))
                if row_key(padded[0], padded[1], padded[2]) in target_keys:
                    print(
                        f"row {index}: {padded[0]} | {padded[1]} | {padded[2]} | "
                        f"Calls(J)={padded[9]} | Transfers(K)={padded[10]} | "
                        f"Talk(S)={padded[18]} | Hold(T)={padded[19]} | AHT(V)={padded[21]}"
                    )
    except HttpError as exc:
        detail = exc.content.decode("utf-8", errors="replace") if exc.content else str(exc)
        raise SystemExit(f"Google Sheets API error:\n{detail}") from exc

    summary.update(
        {
            "spreadsheet_id": args.spreadsheet_id,
            "sheet": args.sheet,
            "agent_key_sheet": args.agent_key_sheet,
            "agent_key_sheet_available": agent_name_map["available"],
            "agent_key_rows": agent_name_map["rows"],
            "mapped_agent_rows": mapped_agent_rows,
            "credentials_source": credentials_source["label"],
            **sync_summary,
        }
    )
    write_summary(args.summary_output, summary)

    action = "Dry run" if args.dry_run else "Synced"
    print(f"{action} {summary['month']} {summary['year']} to '{args.sheet}'")
    print(f"Date range: {summary['start_date']} to {summary['end_date']}")
    print(f"Agent rows from CTM: {summary['agent_rows']}")
    print(
        f"Agent key mappings: {summary['mapped_agent_rows']} "
        f"from '{summary['agent_key_sheet']}'"
    )
    print(f"Updated existing rows: {sync_summary['updated_rows']}")
    print(f"Appended new rows: {sync_summary['appended_rows']}")
    if sync_summary["aht_cells_cleared"]:
        print(f"Cleared AHT cells in column V: {sync_summary['aht_cells_cleared']}")
    if sync_summary["duplicate_existing_rows_skipped"]:
        print(f"Duplicate existing row numbers skipped: {sync_summary['duplicate_existing_rows_skipped']}")
    print(f"Wrote summary: {args.summary_output}")


if __name__ == "__main__":
    main()
