# Domain Finder v2 - one-click deploy
# Replaces the GitHub repo's contents with THIS folder (fresh history),
# sets up the private data repo + deploy key on first run, optionally
# makes the code repo public (free unlimited Actions), and starts a run.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

function Say($m)  { Write-Host $m -ForegroundColor Cyan }
function Good($m) { Write-Host $m -ForegroundColor Green }
function Bad($m)  { Write-Host $m -ForegroundColor Red }
function Quiet($c) { cmd /c "$c >nul 2>nul"; return $LASTEXITCODE }

Write-Host "============================================================"
Write-Host " Domain Finder v2 - deploy"
Write-Host "============================================================"

if (-not (Test-Path ".git")) { Quiet "git init -b master" | Out-Null }

if ((Quiet "gh auth status") -ne 0) {
    Say "GitHub sign-in needed (browser will open)..."
    gh auth login --web -h github.com -p https
    if ($LASTEXITCODE -ne 0) { Bad "Login failed."; exit 1 }
}

function CurrentLogin {
    try { return (gh api user --jq .login 2>$null | Select-Object -First 1).ToString().Trim() }
    catch { return "" }
}

$ghUser = ""
try { $ghUser = (git config --get df.owner 2>$null).ToString().Trim() } catch { }
$who = CurrentLogin
if (-not $ghUser) {
    Say "GitHub CLI is currently signed in as: $who"
    $ans = Read-Host "Is the domain-finder repo on THIS account? Y = yes, N = sign in to the correct account"
    if ($ans -match '^[Nn]') {
        gh auth login --web -h github.com -p https
        if ($LASTEXITCODE -ne 0) { Bad "Login failed."; exit 1 }
        $who = CurrentLogin
    }
    $ghUser = $who
    Quiet "git config df.owner $ghUser" | Out-Null
    Good "Saved: domain-finder lives on '$ghUser'."
} elseif ($who -ne $ghUser) {
    Say "Switching GitHub CLI from '$who' to saved account '$ghUser'..."
    Quiet "gh auth switch --user $ghUser" | Out-Null
    $who = CurrentLogin
    if ($who -ne $ghUser) {
        gh auth login --web -h github.com -p https
        $who = CurrentLogin
        if ($who -ne $ghUser) { Bad "Still on '$who'. Aborting."; exit 1 }
    }
}
Quiet "gh auth setup-git" | Out-Null
$code = "$ghUser/domain-finder"
$data = "$ghUser/domain-finder-data"
Quiet "git config user.name domain-finder" | Out-Null
Quiet "git config user.email domain-finder@users.noreply.github.com" | Out-Null
Quiet "git config core.autocrlf false" | Out-Null

# ---------- 1. private data repo (results live here, never public) ----------
if ((Quiet "gh repo view $data") -ne 0) {
    Say "Creating the private data repo $data ..."
    Quiet "gh repo create $data --private" | Out-Null
    if ((Quiet "gh repo view $data") -ne 0) { Bad "Could not create $data."; exit 1 }
    Good "Created."
}

$seed = Join-Path $env:TEMP ("dfdata_" + [guid]::NewGuid().ToString("N"))
Quiet "git clone https://github.com/$data.git `"$seed`"" | Out-Null
if (-not (Test-Path $seed)) { New-Item -ItemType Directory -Path $seed | Out-Null; Quiet "git -C `"$seed`" init -b main" | Out-Null; Quiet "git -C `"$seed`" remote add origin https://github.com/$data.git" | Out-Null }
$seeded = $false
if (-not (Test-Path (Join-Path $seed "config.json"))) {
    Copy-Item "config.example.json" (Join-Path $seed "config.json")
    $seeded = $true
}
if (Test-Path "dashboard.html") {
    Copy-Item "dashboard.html" (Join-Path $seed "dashboard.html") -Force
    $seeded = $true
}
if (-not (Test-Path (Join-Path $seed "state"))) {
    New-Item -ItemType Directory -Path (Join-Path $seed "state") | Out-Null
    Set-Content -Path (Join-Path $seed "state\.gitkeep") -Value ""
    $seeded = $true
}
if ($seeded) {
    Quiet "git -C `"$seed`" config user.name domain-finder" | Out-Null
    Quiet "git -C `"$seed`" config user.email domain-finder@users.noreply.github.com" | Out-Null
    Quiet "git -C `"$seed`" add -A" | Out-Null
    Quiet "git -C `"$seed`" commit -m seed" | Out-Null
    $db = (git -C "$seed" rev-parse --abbrev-ref HEAD 2>$null)
    if (-not $db) { $db = "main" }
    Quiet "git -C `"$seed`" push -u origin $db" | Out-Null
    Good "Data repo seeded (config + dashboard + state)."
}
Remove-Item -Recurse -Force $seed -ErrorAction SilentlyContinue

# ---------- 2. deploy key so public Actions can write private results ------
$hasKey = $false
try {
    $names = gh secret list --repo $code --json name --jq ".[].name" 2>$null
    if ($names -match "DATA_DEPLOY_KEY") { $hasKey = $true }
} catch { }
if (-not $hasKey) {
    Say "Creating the deploy key that lets the pipeline write results..."
    $kf = Join-Path $env:TEMP ("dfkey_" + [guid]::NewGuid().ToString("N"))
    ssh-keygen -t ed25519 -N '""' -q -f $kf -C "domain-finder-actions" 2>$null
    if (-not (Test-Path "$kf.pub")) {
        ssh-keygen -t ed25519 -N "" -q -f $kf -C "domain-finder-actions"
    }
    if (-not (Test-Path "$kf.pub")) { Bad "ssh-keygen failed (install OpenSSH client)."; exit 1 }
    $pub = Get-Content "$kf.pub" -Raw
    gh api "repos/$data/keys" -f title="domain-finder actions" -f key="$pub" -F read_only=false 1>$null
    if ($LASTEXITCODE -ne 0) { Bad "Could not add the deploy key."; exit 1 }
    Get-Content $kf -Raw | gh secret set DATA_DEPLOY_KEY --repo $code
    if ($LASTEXITCODE -ne 0) { Bad "Could not store the key secret."; exit 1 }
    Remove-Item $kf, "$kf.pub" -Force -ErrorAction SilentlyContinue
    Good "Deploy key installed."
}

# ---------- 3. replace the code repo with this folder (fresh history) ------
Say "Publishing this folder to github.com/$code (replacing everything)..."
Quiet "git remote remove origin" | Out-Null
Quiet "git remote add origin https://github.com/$code.git" | Out-Null
Quiet "git checkout --orphan freshroot" | Out-Null
Quiet "git add -A" | Out-Null
Quiet "git commit -m domain-finder-v2" | Out-Null
Quiet "git branch -D master" | Out-Null
Quiet "git branch -m master" | Out-Null
git push --force origin master
if ($LASTEXITCODE -ne 0) { Bad "Push failed."; exit 1 }
Good "Code published with a clean history. Old contents are gone."

# ---------- 4. make it public (free unlimited Actions minutes) -------------
$vis = ""
try { $vis = (gh repo view $code --json visibility --jq .visibility 2>$null).ToString().Trim() } catch { }
if ($vis -ne "PUBLIC" -and $vis -ne "public") {
    $ans = Read-Host "Make $code PUBLIC now? Required for free unlimited Actions. Y/N"
    if ($ans -match '^[Yy]') {
        gh repo edit $code --visibility public --accept-visibility-change-consequences 2>$null
        if ($LASTEXITCODE -ne 0) { gh repo edit $code --visibility public }
        if ($LASTEXITCODE -eq 0) { Good "Repo is now public. Results stay in the private data repo." }
        else { Bad "Could not change visibility; do it in repo Settings -> Danger Zone." }
    } else {
        Say "Left private. Note: daily runs need ~1 hour/day, over the private free quota."
    }
}

# ---------- 5. first run --------------------------------------------------
Say "Starting the first pipeline run (zones baseline + bootstrap)..."
gh workflow run daily.yml --repo $code -f force_bootstrap=1 2>$null
if ($LASTEXITCODE -ne 0) {
    Start-Sleep -Seconds 20      # workflow registration can lag a fresh push
    gh workflow run daily.yml --repo $code -f force_bootstrap=1 2>$null
    if ($LASTEXITCODE -ne 0) {
        Say "Could not start it from here; it will run on tonight's schedule,"
        Say "or press 'Run workflow' on the Actions page."
    }
}

Write-Host "============================================================"
Good " Done. Watch: https://github.com/$code/actions"
Good " Dashboard: open dashboard.html, repo $data + a fine-grained"
Good " token with Contents: Read-only on domain-finder-data."
Write-Host "============================================================"
