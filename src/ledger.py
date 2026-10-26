"""The dying-domains ledger.

Every name that leaves a zone and passes the name rules is recorded with
the date it vanished. Screening then promotes or drops entries through
stages, all persisted between runs (Actions cache, with a weekly backup
in the private data repo so a cache eviction never loses the pipeline).

Stages:
  new      just left the zone; awaiting the Open PageRank screen
  pass     has real PageRank; awaiting the Wayback vet
  watch    vetted + scored above the bar; RDAP-watched until it drops
  held     watched past max_watch_days and still registered (weekly check)
  alerted  dropped and alerted; kept to prevent duplicate alerts
  oprfail / vetfail / low / gone   terminal; pruned after a few days

File: ledger/<tld>.csv.gz with columns
  domain,gone,stage,opr,ref,first_year,score,dr,last
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import os

LEDGER_DIR = "ledger"
FIELDS = ["domain", "gone", "stage", "opr", "ref", "first_year",
          "score", "dr", "last"]
TERMINAL = {"oprfail", "vetfail", "low", "gone"}
TERMINAL_KEEP_DAYS = 7
ALERTED_KEEP_DAYS = 400
QUEUE_STALE_DAYS = 45      # new/pass never screened by now missed the window


def _path(tld: str) -> str:
    return os.path.join(LEDGER_DIR, f"{tld}.csv.gz")


def load(tld: str, backup_dir: str | None = None) -> dict[str, dict]:
    """Load the ledger; fall back to the data-repo backup on cache loss."""
    path = _path(tld)
    if not os.path.exists(path) and backup_dir:
        alt = os.path.join(backup_dir, f"{tld}.csv.gz")
        if os.path.exists(alt):
            os.makedirs(LEDGER_DIR, exist_ok=True)
            with open(alt, "rb") as a, open(path, "wb") as b:
                b.write(a.read())
    if not os.path.exists(path):
        return {}
    out: dict[str, dict] = {}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            d = row.get("domain")
            if d:
                out[d] = row
    return out


def save(tld: str, entries: dict[str, dict]) -> None:
    os.makedirs(LEDGER_DIR, exist_ok=True)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore")
    w.writeheader()
    for d in sorted(entries):
        row = {k: entries[d].get(k, "") for k in FIELDS}
        row["domain"] = d
        w.writerow(row)
    with gzip.open(_path(tld), "wt", encoding="utf-8") as f:
        f.write(buf.getvalue())


def _age_days(entry: dict, today: dt.date) -> int:
    try:
        return (today - dt.date.fromisoformat(entry.get("gone", ""))).days
    except ValueError:
        return 0


def update(tld: str, entries: dict[str, dict], removed_path: str | None,
           added_path: str | None, cfg: dict, name_ok, today: dt.date,
           log) -> dict:
    """Apply one day's zone diff. Returns counts for the dashboard."""
    stats = {"removed_nameok": 0, "reregistered": 0, "pruned": 0}
    iso = today.isoformat()

    if removed_path and os.path.exists(removed_path):
        with open(removed_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                d = line.strip()
                if not d or d in entries:
                    continue
                if name_ok(d, cfg):
                    entries[d] = {"domain": d, "gone": iso, "stage": "new",
                                  "last": iso}
                    stats["removed_nameok"] += 1

    if added_path and os.path.exists(added_path):
        with open(added_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                d = line.strip()
                e = entries.get(d)
                if e and e.get("stage") not in ("alerted",):
                    e["stage"] = "gone"          # someone registered it
                    e["last"] = iso
                    stats["reregistered"] += 1

    # prune terminals and stale alerted entries
    for d in list(entries):
        e = entries[d]
        stage = e.get("stage", "")
        age = _age_days(e, today)
        if stage in TERMINAL and age > 0:
            last = e.get("last") or e.get("gone") or iso
            try:
                since = (today - dt.date.fromisoformat(last)).days
            except ValueError:
                since = TERMINAL_KEEP_DAYS + 1
            if since >= TERMINAL_KEEP_DAYS:
                del entries[d]
                stats["pruned"] += 1
        elif stage == "alerted" and age > ALERTED_KEEP_DAYS:
            del entries[d]
            stats["pruned"] += 1
        elif stage in ("new", "pass") and age > QUEUE_STALE_DAYS:
            # the budget never reached it and the drop window has passed;
            # without this the queue grows without bound
            del entries[d]
            stats["pruned"] += 1

    log(f"[{tld}] ledger: +{stats['removed_nameok']} new, "
        f"{stats['reregistered']} re-registered, {stats['pruned']} pruned")
    return stats


def counts(entries: dict[str, dict]) -> dict:
    out: dict[str, int] = {}
    for e in entries.values():
        s = e.get("stage", "?")
        out[s] = out.get(s, 0) + 1
    out["total"] = len(entries)
    return out
