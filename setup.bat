@echo off
setlocal enabledelayedexpansion
title Domain Finder - one-click setup
echo ============================================================
echo  Domain Finder - setup
echo  Creates a PRIVATE GitHub repo, uploads the tool, stores
echo  your keys as repo secrets, and starts the cloud jobs.
echo  Nothing heavy ever runs or downloads on this PC.
echo ============================================================
echo.

where git >nul 2>nul
if errorlevel 1 (
  echo [!] Git is not installed. Installing with winget...
  winget install --id Git.Git -e --silent
  echo     Close this window and run setup.bat again after Git finishes.
  pause & exit /b 1
)

where gh >nul 2>nul
if errorlevel 1 (
  echo [!] GitHub CLI is not installed. Installing with winget...
  winget install --id GitHub.cli -e --silent
  echo     Close this window and run setup.bat again after it finishes.
  pause & exit /b 1
)

gh auth status >nul 2>nul
if errorlevel 1 (
  echo [1/5] Sign in to GitHub (a browser window will open)...
  gh auth login --web -h github.com -p https
  if errorlevel 1 ( echo Login failed. & pause & exit /b 1 )
) else (
  echo [1/5] GitHub login found.
)

set REPO=domain-finder
echo [2/5] Creating private repo "%REPO%" and uploading the tool...
git init -q 2>nul
git add -A
git commit -q -m "domain finder" 2>nul
gh repo view %REPO% >nul 2>nul
if errorlevel 1 (
  gh repo create %REPO% --private --source . --push
) else (
  for /f "tokens=*" %%u in ('gh api user --jq .login') do set GHUSER=%%u
  git remote remove origin 2>nul
  git remote add origin https://github.com/!GHUSER!/%REPO%.git
  git push -u origin HEAD --force
)

echo.
echo [3/5] Alert and API keys. Press Enter to skip any optional one.
echo.
set /p ALERT_TO="Email address to receive alerts: "
set /p GMAIL_USER="Gmail address used to SEND the alerts: "
set /p GMAIL_APP_PASSWORD="Gmail app password (Google Account - Security - App passwords): "
set /p AHREFS_API_KEY="Ahrefs APIv3 key (free account - Account settings - API keys): "
set /p OPR_API_KEY="Open PageRank key (openpagerank.com, free): "
set /p GOOGLE_CSE_KEY="(optional) Google Custom Search API key: "
set /p GOOGLE_CSE_CX="(optional) Google Programmable Search Engine cx id: "
set /p MOZ_ACCESS_ID="(optional) Moz access id: "
set /p MOZ_SECRET_KEY="(optional) Moz secret key: "

echo [4/5] Storing keys as encrypted repo secrets...
for %%k in (ALERT_TO GMAIL_USER GMAIL_APP_PASSWORD AHREFS_API_KEY OPR_API_KEY GOOGLE_CSE_KEY GOOGLE_CSE_CX MOZ_ACCESS_ID MOZ_SECRET_KEY) do (
  if not "!%%k!"=="" gh secret set %%k --body "!%%k!"
)

echo [5/5] Starting the watchlist build in the cloud...
gh workflow run build-watchlist.yml
gh workflow run monitor.yml

echo.
echo ============================================================
echo  Done. The watchlist builds itself over the next 1-2 days
echo  (it re-runs every 4 hours in the cloud until complete).
echo  After that, the monitor runs daily at 7:41am your time and
echo  emails you only when a genuinely good domain is dying.
echo  Progress: gh run list   or the Actions tab of the repo.
echo ============================================================
pause
