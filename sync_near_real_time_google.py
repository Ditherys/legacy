import argparse
import calendar
import json
import os
from pathlib import Path

from googleapiclient.errors import HttpError

import ctm_kpi_raw_dry_run as ctm
import sync_kpi_raw_google as kpi_sync


DEFAULT_SHEET_NAME = "Near_Real_Time"
HEADERS = [
    "Year",
    "Month",
    "Agent",
    "Calls",
    "Transfers",
    "Talk Time",
    "Hold Time",
    "AHT",
    "Last Updated",
    "Outbound Calls",
]


def parse_duration(value):
    parts = str(value or "0:00:00").split(":")
    if len(parts) != 3:
        return 0
    hours, minutes, seconds = parts
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds)


def near_real_time_values(rows):
    values = []
    for row in rows:
        calls = int(row.get("Calls") or 0)
        outbound_calls = int(row.get("Outbound Calls") or 0)
        talk_time = row.get("Talk Time") or "0:00:00"
        hold_time = row.get("Hold Time") or "0:00:00"
        aht = ""
        total_calls = calls + outbound_calls
        if total_calls:
            aht = ctm.hms(round((parse_duration(talk_time) + parse_duration(hold_time)) / total_calls))

        values.append(
            [
                row["Year"],
                row["Month"],
                row["Agent"],
                calls,
                row.get("Transfers") or 0,
                talk_time,
                hold_time,
                aht,
                row["Last Updated"],
                row.get("Outbound Calls") or 0,
            ]
        )
    return values


def parse_args(argv=None):
    ctm_today = ctm.datetime.now(ctm.ZoneInfo(ctm.TIMEZONE_NAME)).date()
    parser = argparse.ArgumentParser(description="Sync CTM metrics to the Near_Real_Time Google Sheet tab.")
    parser.add_argument(
        "--spreadsheet-id",
        default=os.getenv("GOOGLE_SPREADSHEET_ID", kpi_sync.DEFAULT_SPREADSHEET_ID),
    )
    parser.add_argument("--sheet", default=os.getenv("GOOGLE_NEAR_REAL_TIME_SHEET", DEFAULT_SHEET_NAME))
    parser.add_argument(
        "--agent-key-sheet",
        default=os.getenv("GOOGLE_AGENT_KEY_SHEET", kpi_sync.DEFAULT_AGENT_KEY_SHEET),
    )
    parser.add_argument("--credentials", default=os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE"))
    parser.add_argument("--year", type=int, default=ctm_today.year)
    parser.add_argument("--month", type=kpi_sync.month_number, default=ctm_today.month)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--active-only", action="store_true")
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument(
        "--no-refresh-end-date",
        action="store_true",
        help="Use cached rows for the final date too. Not recommended for near-real-time runs.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--show-rows", action="store_true")
    parser.add_argument("--summary-output", default=str(ctm.OUTPUT_DIR / "near_real_time_sync_summary.json"))
    args = parser.parse_args(argv)
    args.refresh_end_date = not args.no_refresh_end_date
    return args


def validate_headers(service, spreadsheet_id, sheet_name):
    values = kpi_sync.get_sheet_values(service, spreadsheet_id, sheet_name, "A1:J1")
    if not values:
        raise SystemExit(f"Sheet tab '{sheet_name}' is missing headers in A1:J1.")

    actual = kpi_sync.pad(values[0], len(HEADERS))[: len(HEADERS)]
    if actual != HEADERS:
        raise SystemExit(
            f"'{sheet_name}' headers do not match expected A:J layout.\n"
            f"Expected: {HEADERS}\n"
            f"Actual:   {actual}"
        )


def _row_key(year, month, agent):
    return (str(year).strip().lower(), str(month).strip().lower(), str(agent).strip().lower())


def write_values(service, spreadsheet_id, sheet_name, values, dry_run=False):
    """
    Upserts rows by (Year, Month, Agent) key.
    Rows from other months are preserved — only the incoming month's rows are touched.
    New rows are appended; existing matching rows are updated in-place.
    """
    if dry_run:
        return {"updated": 0, "appended": 0}

    existing = kpi_sync.get_sheet_values(service, spreadsheet_id, sheet_name, "A2:J")

    # Map (year, month, agent) → 1-based sheet row number (row 2 is the first data row)
    row_map = {}
    for idx, row in enumerate(existing):
        padded = kpi_sync.pad(row, 3)
        if not any(str(c).strip() for c in padded[:3]):
            continue
        key = _row_key(padded[0], padded[1], padded[2])
        if key not in row_map:
            row_map[key] = idx + 2

    updates = []
    appends = []
    for value in values:
        key = _row_key(value[0], value[1], value[2])
        row_number = row_map.get(key)
        if row_number:
            updates.append({
                "range": kpi_sync.sheet_range(sheet_name, f"A{row_number}:J{row_number}"),
                "values": [value],
            })
        else:
            appends.append(value)

    if updates:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": updates},
        ).execute()

    if appends:
        service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=kpi_sync.sheet_range(sheet_name, "A2:J"),
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": appends},
        ).execute()

    return {"updated": len(updates), "appended": len(appends)}


def main(argv=None):
    kpi_sync.load_dotenv()
    args = parse_args(argv)
    args.spreadsheet_id = kpi_sync.parse_spreadsheet_id(args.spreadsheet_id)
    credentials_source = kpi_sync.find_credentials_source(args.credentials)

    try:
        service = kpi_sync.build_sheets_service(credentials_source)
        rows, summary = kpi_sync.generate_kpi_rows(args)

        agent_key_values = kpi_sync.get_optional_sheet_values(
            service,
            args.spreadsheet_id,
            args.agent_key_sheet,
            "A:D",
        )
        agent_name_map = kpi_sync.build_agent_name_map(agent_key_values)
        mapped_agent_rows = kpi_sync.apply_agent_name_map(rows, agent_name_map)
        values = near_real_time_values(rows)

        validate_headers(service, args.spreadsheet_id, args.sheet)
        write_result = write_values(service, args.spreadsheet_id, args.sheet, values, dry_run=args.dry_run)
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
            "written_rows": len(values),
            "updated_rows": (write_result or {}).get("updated", 0),
            "appended_rows": (write_result or {}).get("appended", 0),
            "dry_run": args.dry_run,
        }
    )

    output_path = Path(args.summary_output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    action = "Dry run" if args.dry_run else "Synced"
    print(f"{action} {calendar.month_name[args.month]} {args.year} to '{args.sheet}'")
    print(f"Date range: {summary['start_date']} to {summary['end_date']}")
    print(f"Rows written: {len(values)} (updated: {summary['updated_rows']}, appended: {summary['appended_rows']})")
    print(f"Agent key mappings: {mapped_agent_rows} from '{args.agent_key_sheet}'")
    if args.show_rows:
        for value in values:
            print(" | ".join(str(cell) for cell in value))
    print(f"Wrote summary: {args.summary_output}")


if __name__ == "__main__":
    main()
