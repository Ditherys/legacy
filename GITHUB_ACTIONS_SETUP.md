# GitHub Actions Setup

This repo can run the `Near_Real_Time` sync every 5 minutes using GitHub
Actions. Keep the repo public if you want standard GitHub-hosted runner minutes
to be free.

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

## 4. Run and monitor

Go to:

```text
Repo > Actions > Near Real Time CTM Sync
```

Click `Run workflow` once to test. After that, the schedule runs every 5 minutes.
