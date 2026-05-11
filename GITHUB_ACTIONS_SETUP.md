# GitHub Actions Setup

This repo runs the `Near_Real_Time` sync in GitHub Actions. The timer is handled
by cron-job.org, which calls the workflow dispatch API every 5 minutes.

The workflow restores CTM daily cache between runs. The current CTM date still
refreshes every run so the live sheet does not go stale.

## 1. Push only safe files

These are ignored and should not be committed:

```text
.env.local
secrets/
output/
*.xlsx
*.xls
```

## 2. Create repository secrets

In GitHub:

```text
Repo > Settings > Secrets and variables > Actions > New repository secret
```

Create these secrets:

```text
CTM_ACCESS_KEY
CTM_SECRET_KEY
CTM_ACCOUNT_ID
GOOGLE_SPREADSHEET_ID
GOOGLE_SERVICE_ACCOUNT_JSON_B64
```

Use this PowerShell command locally to generate the service account secret:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\Users\D_Reyes\Desktop\legacy\secrets\root-bricolage-494814-e1-e0e35ae66b7b.json"))
```

Copy the output into the `GOOGLE_SERVICE_ACCOUNT_JSON_B64` secret.

## 3. Push the repo

```powershell
git init
git add .gitignore .github requirements.txt *.py *.bat README.md GITHUB_ACTIONS_SETUP.md
git commit -m "Add near real-time CTM sync"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

## 4. Run manually first

Go to:

```text
Repo > Actions > Near Real Time CTM Sync
```

Click `Run workflow` once to test.

## 5. Create the cron-job.org job

Create a fine-grained GitHub token:

```text
GitHub > Settings > Developer settings > Personal access tokens > Fine-grained tokens
```

Use:

```text
Repository access: Ditherys/legacy only
Repository permissions: Actions = Read and write
```

In cron-job.org:

```text
URL: https://api.github.com/repos/Ditherys/legacy/actions/workflows/near-real-time-sync.yml/dispatches
Method: POST
Schedule: Every 5 minutes
```

Headers:

```text
Accept: application/vnd.github+json
Authorization: Bearer YOUR_GITHUB_TOKEN
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
```

Request body:

```json
{"ref":"main"}
```

GitHub returns `204 No Content` on a successful dispatch, so an empty response is
expected.
