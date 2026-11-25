"""Build the watchlist of pre-qualified domains.

Runs in GitHub Actions (never on the owner's own connection). Resumable:
each run processes a chunk of CDX checks, commits progress, and the next
run continues until the list is complete.

Pipeline:
  1. Download the Majestic Million CSV (free daily export of the top 1M
     domains by referring subnets). ~75MB, downloaded on the runner.
  2. Keep .com/.net/.org, apply name rules and the hard-DQ lexicon.
     This yields the candidate pool (capped).
  3. For each candidate (in chunks), one cheap Wayback CDX call:
     require age, capture count, year coverage, and recent-enough captures.
  4. Survivors land in state/watchlist.txt with their birth year.

State files (committed to the repo by the workflow):
  state/candidates.txt   name-filtered pool, one domain per line
  state/cdx_cursor.txt   index of the next candidate to CDX-check
  state/watchlist.txt    final watched domains: "domain,first_year,months"
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import time
import urllib.request

from . import lexicons, vet

MAJESTIC_URL = "https://downloads.majestic.com/majestic_million.csv"
STATE = "state"


def _cfg() -> dict:
    return json.load(open("config.json"))


def _name_ok(domain: str, cfg: dict) -> bool:
    try:
        name, tld = domain.rsplit(".", 1)
    except ValueError:
        return False
    rules = cfg["name_rules"]
    if tld not in cfg["tlds"]:
        return False
    if not (rules["min_length"] <= len(name) <= rules["max_length"]):
        return False
    if rules["letters_only"] and not re.fullmatch(r"[a-z]+", name):
        return False
    if lexicons.name_is_disqualified(name):
        return False
    return True


def step1_candidates() -> None:
    cfg = _cfg()
    cap = cfg["watchlist"]["max_candidates_after_name_filter"]
    top_n = cfg["watchlist"]["majestic_top_n"]
    print("downloading majestic million...")
    req = urllib.request.Request(MAJESTIC_URL, headers={"User-Agent": vet.UA})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read().decode("utf-8", "replace")
    out: list[str] = []
    reader = csv.DictReader(io.StringIO(data))
    for i, row in enumerate(reader):
        if i >= top_n or len(out) >= cap:
            break
        d = (row.get("Domain") or "").strip().lower()
        if d and _name_ok(d, cfg):
            out.append(d)
    os.makedirs(STATE, exist_ok=True)
    with open(f"{STATE}/candidates.txt", "w") as f:
        f.write("\n".join(out))
    with open(f"{STATE}/cdx_cursor.txt", "w") as f:
        f.write("0")
    print(f"candidates: {len(out)}")


def step2_cdx_chunk() -> None:
    cfg = _cfg()
    w = cfg["watchlist"]
    cands = [l.strip() for l in open(f"{STATE}/candidates.txt") if l.strip()]
    cursor = int(open(f"{STATE}/cdx_cursor.txt").read().strip() or 0)
    chunk = w["cdx_chunk_per_run"]
    end = min(len(cands), cursor + chunk)
    kept: list[str] = []
    print(f"cdx-checking candidates {cursor}..{end} of {len(cands)}")
    for d in cands[cursor:end]:
        time.sleep(0.35)          # ~3 req/s: polite to archive.org
        s = vet.cdx_summary(d)
        if not s:
            continue
        if (s["age_years"] >= w["min_first_year_age"]
                and s["months_captured"] >= min(w["min_captures"], 60)
                and s["coverage"] >= w["min_year_coverage"]
                and s["years_since_last"] <= w["max_years_since_last_capture"]):
            kept.append(f"{d},{s['first_year']},{s['months_captured']}")
    with open(f"{STATE}/watchlist.txt", "a") as f:
        for line in kept:
            f.write(line + "\n")
    with open(f"{STATE}/cdx_cursor.txt", "w") as f:
        f.write(str(end))
    done = end >= len(cands)
    print(f"kept {len(kept)} this run; cursor={end}; done={done}")
    # expose completion to the workflow
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"done={'true' if done else 'false'}\n")


if __name__ == "__main__":
    import sys
    if "--candidates" in sys.argv:
        step1_candidates()
    else:
        if not os.path.exists(f"{STATE}/candidates.txt"):
            step1_candidates()
        step2_cdx_chunk()
