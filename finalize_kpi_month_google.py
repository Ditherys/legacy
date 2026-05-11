import argparse
import calendar

import ctm_kpi_raw_dry_run as ctm
import sync_kpi_raw_google


def month_number(value):
    return ctm.month_number(str(value))


def parse_args():
    ctm_today = ctm.datetime.now(ctm.ZoneInfo(ctm.TIMEZONE_NAME)).date()
    parser = argparse.ArgumentParser(description="Finalize a full CTM KPI month in Google Sheets.")
    parser.add_argument("--year", type=int, default=ctm_today.year)
    parser.add_argument("--month", type=month_number, default=ctm_today.month)
    parser.add_argument("--spreadsheet-id")
    parser.add_argument("--sheet")
    parser.add_argument("--agent-key-sheet")
    parser.add_argument("--credentials")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-refresh", action="store_true", help="Refresh cached CTM call rows before finalizing.")
    parser.add_argument("--include-zero-agents", action="store_true", help="Include agents with no CTM activity.")
    return parser.parse_args()


def main():
    args = parse_args()
    last_day = calendar.monthrange(args.year, args.month)[1]
    month_name = calendar.month_name[args.month]
    argv = [
        "--year",
        str(args.year),
        "--month",
        str(args.month),
        "--start-date",
        f"{args.year}-{args.month:02d}-01",
        "--end-date",
        f"{args.year}-{args.month:02d}-{last_day:02d}",
        "--summary-output",
        str(ctm.OUTPUT_DIR / f"kpi_google_finalize_{month_name.lower()}_{args.year}.json"),
    ]

    if not args.include_zero_agents:
        argv.append("--active-only")
    if args.force_refresh:
        argv.append("--force-refresh")
    if args.dry_run:
        argv.append("--dry-run")
    if args.spreadsheet_id:
        argv.extend(["--spreadsheet-id", args.spreadsheet_id])
    if args.sheet:
        argv.extend(["--sheet", args.sheet])
    if args.agent_key_sheet:
        argv.extend(["--agent-key-sheet", args.agent_key_sheet])
    if args.credentials:
        argv.extend(["--credentials", args.credentials])

    sync_kpi_raw_google.main(argv)


if __name__ == "__main__":
    main()
