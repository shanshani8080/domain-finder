"""CZDS zone download and day-over-day diff, at .com scale.

The .com zone is ~160M names (a multi-GB gzip), far too large for Python
sets in runner memory. So everything is file-based:

  1. Stream the zone download through zcat/awk into a names-only file
     (one second-level name per line), done at C speed by a shell pipeline.
     A pure-Python streaming fallback covers environments without the tools.
  2. LC_ALL=C sort -u the names file on disk.
  3. comm(1) against yesterday's sorted baseline:
       removed = names that LEFT the zone (entered the deletion pipeline)
       added   = names that came (back) into the zone (registered)
  4. Replace the baseline (stored gzipped in the Actions cache).

Nothing here prints domain names: this module runs in a public repo, and
its logs are public. Counts only.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request

UA = "domain-finder/2.0 (personal research tool)"
AUTH_URL = "https://account-api.icann.org/api/authenticate"
LINKS_URL = "https://czds-api.icann.org/czds/downloads/links"
CACHE_DIR = "zonecache"


# ----------------------------------------------------------- czds client ----
def czds_token() -> str:
    user = os.environ.get("CZDS_USERNAME")
    pw = os.environ.get("CZDS_PASSWORD")
    if not user or not pw:
        raise SystemExit("CZDS_USERNAME / CZDS_PASSWORD secrets are not set")
    body = json.dumps({"username": user, "password": pw}).encode()
    req = urllib.request.Request(
        AUTH_URL, data=body,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode())
    tok = data.get("accessToken")
    if not tok:
        raise SystemExit("CZDS login failed: no accessToken in response")
    return tok


def zone_url(tld: str, token: str) -> str:
    hdr = {"Authorization": "Bearer " + token, "User-Agent": UA}
    req = urllib.request.Request(LINKS_URL, headers=hdr)
    with urllib.request.urlopen(req, timeout=60) as r:
        links = json.loads(r.read().decode())
    url = next((l for l in links if l.lower().endswith(f"/{tld}.zone")), None)
    if not url:
        have = ", ".join(l.rsplit("/", 1)[-1] for l in links) or "none"
        raise SystemExit(f"no approved CZDS access to the {tld} zone "
                         f"(approved: {have})")
    return url


# ------------------------------------------------------- names extraction ----
def _tools_available() -> bool:
    return all(shutil.which(t) for t in ("curl", "zcat", "awk", "sort", "comm"))


def _extract_shell(url: str, token: str, tld: str, out_path: str,
                   tmp_dir: str) -> None:
    """curl | zcat | awk -> unique-consecutive second-level names."""
    cfgfile = os.path.join(tmp_dir, "curlcfg")
    with open(cfgfile, "w") as f:
        f.write(f'header = "Authorization: Bearer {token}"\n')
        f.write(f'header = "User-Agent: {UA}"\n')
        f.write("silent\nshow-error\nfail\nlocation\n")
        f.write("retry = 2\nretry-delay = 20\n")
    # awk: first field, lowercased; keep single-label names under .<tld>.;
    # suppress consecutive repeats (zone rows for one name are adjacent).
    awk = (
        '{ n = tolower($1) }'
        f' n ~ /^[a-z0-9-]+\\.{tld}\\.$/'
        ' { sub(/\\.$/, "", n); if (n != p) { p = n; print n } }'
    )
    cmd = (f"set -o pipefail; curl --config {cfgfile} '{url}' "
           f"| zcat | awk '{awk}' > {out_path}")
    env = dict(os.environ, LC_ALL="C")
    subprocess.run(["bash", "-c", cmd], check=True, env=env)
    os.remove(cfgfile)


def _extract_python(url: str, token: str, tld: str, out_path: str) -> None:
    """Pure-Python streaming fallback (slower; fine for .net/.org sizes)."""
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "User-Agent": UA})
    suffix = ("." + tld + ".").encode()
    prev = b""
    with urllib.request.urlopen(req, timeout=1800) as r, \
            open(out_path, "wb") as out:
        gz = gzip.GzipFile(fileobj=r)
        for line in gz:
            if not line or line[0] in b";$ \t":
                continue
            tok = line.split(None, 1)[0].lower()
            if tok.endswith(suffix):
                name = tok[: -len(suffix)]
                if name and b"." not in name and name != prev:
                    prev = name
                    out.write(name + b"." + tld.encode() + b"\n")


def _sort_unique(src: str, dest: str, tmp_dir: str) -> None:
    env = dict(os.environ, LC_ALL="C")
    subprocess.run(
        ["sort", "-u", "-S", "35%", "-T", tmp_dir, "-o", dest, src],
        check=True, env=env)


def _count_lines(path: str) -> int:
    out = subprocess.run(["wc", "-l", path], check=True,
                         capture_output=True, text=True)
    return int(out.stdout.split()[0])


# ------------------------------------------------------------- refresh ------
def refresh(tld: str, token: str, work_dir: str = "ztmp",
            cache_dir: str = CACHE_DIR) -> dict:
    """Download today's zone, diff against the cached baseline.

    Returns {"count", "removed_path"|None, "added_path"|None,
             "removed", "added", "first_run"} and replaces the baseline.
    """
    os.makedirs(work_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(dir=work_dir)
    raw = os.path.join(tmp, f"{tld}.raw")
    today = os.path.join(work_dir, f"{tld}.today")

    url = zone_url(tld, token)
    if _tools_available():
        _extract_shell(url, token, tld, raw, tmp)
    else:
        _extract_python(url, token, tld, raw)
    _sort_unique(raw, today, tmp)
    os.remove(raw)
    count = _count_lines(today)
    if count < 1000:
        raise SystemExit(f"{tld} zone parse produced only {count} names; "
                         "aborting so yesterday's baseline is kept")

    baseline = os.path.join(cache_dir, f"{tld}.names.gz")
    result = {"count": count, "removed_path": None, "added_path": None,
              "removed": 0, "added": 0, "first_run": True}

    if os.path.exists(baseline):
        removed = os.path.join(work_dir, f"{tld}.removed")
        added = os.path.join(work_dir, f"{tld}.added")
        env = dict(os.environ, LC_ALL="C")
        subprocess.run(
            ["bash", "-c",
             f"set -o pipefail; comm -23 <(zcat {baseline}) {today} > {removed}"],
            check=True, env=env)
        subprocess.run(
            ["bash", "-c",
             f"set -o pipefail; comm -13 <(zcat {baseline}) {today} > {added}"],
            check=True, env=env)
        result.update(first_run=False,
                      removed_path=removed, removed=_count_lines(removed),
                      added_path=added, added=_count_lines(added))
        # Sanity: a corrupt/partial download would show up as a huge removal
        # wave. Refuse to treat >5% of the zone as deleted in one day.
        if result["removed"] > max(50_000, count * 0.05):
            raise SystemExit(
                f"{tld}: {result['removed']:,} removals in one day looks like "
                "a bad download; keeping yesterday's baseline")

    subprocess.run(["bash", "-c", f"gzip -c {today} > {baseline}.new"],
                   check=True)
    os.replace(baseline + ".new", baseline)
    os.remove(today)
    shutil.rmtree(tmp, ignore_errors=True)
    return result
