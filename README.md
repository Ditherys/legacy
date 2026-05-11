# CTM Legacy Checker

Clean CTM investigation workspace for tracing Agent Activity metrics.

## Endpoint

The Agent Activity rawfile-style totals come from:

```text
GET https://api.calltrackingmetrics.com/api/v1/accounts/{account_id}/agents/utilization.json
```

Rawfile mapping:

```text
Calls      -> metrics.talk_time[].count, if the rawfile column is a generic "Calls"
Inbound    -> metrics.inbound_calls[].count, if the rawfile column is "Inbound calls"
Talk Time  -> metrics.talk_time[].total
Hold Time  -> metrics.hold_time[].total
AHT        -> (Talk Time + Hold Time) / Calls
```

Transfer mapping:

```text
GET https://api.calltrackingmetrics.com/api/v1/accounts/{account_id}/calls.json

Filters used for Rae March transfer audit:
start_date=2026-03-01
end_date=2026-03-31
direction=inbound
status=answered
filter=agent.name:Rae Sagun

Transfer count source:
calls[].transfers[] where transfers[].from is Rae's CTM user SID

For Rae and Ella, the provided transfer totals match first-time-caller transfer
calls only. When repeat callers are included, the raw calls endpoint returns a
higher transfer count.
```

The script also checks the agent email through:

```text
GET https://api.calltrackingmetrics.com/api/v1/accounts/{account_id}/users.json
```

## Run

Use the date range from the CTM rawfile:

```powershell
python .\ctm_agent_metric_trace.py 2026-02-18 2026-02-18
```

To compare against Rae Sagun's rawfile totals:

```powershell
python .\ctm_agent_metric_trace.py 2026-02-18 2026-02-18 --expected-calls 171 --expected-talk-time 12:27:34 --expected-hold-time 0:39:55
```

If the CTM UI rawfile was filtered by a team, pass that team id too:

```powershell
python .\ctm_agent_metric_trace.py 2026-02-18 2026-02-18 --team-id 5457
```

## KPI Raw Google Sheets sync

The KPI Raw sync writes CTM-owned columns to the `KPI Raw` tab and preserves
manual/formula columns such as QA, Attendance, Transfer Rate, Admission Rate,
VOB Rate, and AHT.

Optional agent name mapping is read from a tab named `Primary Key`. Use these
headers:

```text
CTM_Agent | Agent
```

`CTM_Agent` should match the CTM name. `Agent` is written to `KPI Raw` column C.

```powershell
pip install -r .\requirements.txt
python .\sync_kpi_raw_google.py --year 2026 --month May --active-only --dry-run
python .\sync_kpi_raw_google.py --year 2026 --month May --active-only
```

Finalize a full month after month-end:

```powershell
python .\finalize_kpi_month_google.py --year 2026 --month May --force-refresh
```

## Near real-time Google Sheets sync

The cron runner writes a live snapshot to `Near_Real_Time`, preserving only row
1 headers and replacing rows 2+ on each run.

Expected headers:

```text
Year | Month | Agent | Calls | Transfers | Talk Time | Hold Time | AHT | Last Updated
```

`Agent` comes from the `Agents`/`Agent` column in the `Primary Key` tab.

```powershell
python .\sync_near_real_time_google.py --active-only --dry-run
python .\sync_near_real_time_google.py --active-only
```

## GitHub Actions schedule

The workflow in `.github/workflows/near-real-time-sync.yml` runs every 5 minutes
and can also be run manually from the Actions tab.
It restores the CTM daily cache to avoid refetching old days every run, while
the current CTM date is refreshed on each near-real-time run.

Required repository secrets:

```text
CTM_ACCESS_KEY
CTM_SECRET_KEY
CTM_ACCOUNT_ID
GOOGLE_SPREADSHEET_ID
GOOGLE_SERVICE_ACCOUNT_JSON_B64
```

Generate the Google service account JSON secret locally with PowerShell:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\Users\D_Reyes\Desktop\legacy\secrets\root-bricolage-494814-e1-e0e35ae66b7b.json"))
```
