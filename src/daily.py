"""Daily orchestrator. One run does everything:

  zones -> ledger -> screening -> drop alerts -> bootstrap (first run /
  weekly) -> dashboard feed.

Privacy: this file runs in a PUBLIC repo, so stdout carries counts only.
Domain names and findings are written to the private data repo:
  <data>/state/results.json   the dashboard feed
  <data>/state/alerted.json   duplicate-alert guard
  <data>/state/run.log        detailed, name-level log
The data checkout location comes from env DATA_DIR (default: data).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import time
import traceback

from . import bootstrap, ledger, namerules, screen, zones

RESULTS_KEEP_AVAILABLE = 60
RESULTS_KEEP_ALERTS = 30


class Log:
    """Appends name-level detail to the PRIVATE run log only."""
    MAX_BYTES = 400_000

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > self.MAX_BYTES:
            tail = open(path, "rb").read()[-self.MAX_BYTES // 2:]
            with open(path, "wb") as f:
                f.write(b"(older log trimmed)\n" + tail)
        self.f = open(path, "a", encoding="utf-8")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        self.f.write(f"\n==== run {stamp}Z ====\n")

    def __call__(self, msg: str) -> None:
        self.f.write(msg + "\n")
        self.f.flush()


def _load_json(path: str, default):
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            return default
    return default


def run() -> None:
    t0 = time.time()
    data_dir = os.environ.get("DATA_DIR", "data")
    state_dir = os.path.join(data_dir, "state")
    os.makedirs(state_dir, exist_ok=True)
    cfg_path = os.path.join(data_dir, "config.json")
    if not os.path.exists(cfg_path):
        raise SystemExit("config.json missing in the data repo; run the "
                         "updater once to seed it")
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    log = Log(os.path.join(state_dir, "run.log"))

    prev = _load_json(os.path.join(state_dir, "results.json"), {})
    available = list(prev.get("available_now", []))
    alerts_hist = list(prev.get("alerts", []))
    alerted = _load_json(os.path.join(state_dir, "alerted.json"), {})

    today = dt.date.today()
    zstats: dict[str, dict] = {}
    all_entries: dict[str, dict] = {}
    notes: list[str] = []
    backup_dir = os.path.join(state_dir, "ledger-backup")

    token = zones.czds_token()
    any_diff = False
    for tld in cfg["tlds"]:
        try:
            r = zones.refresh(tld, token)
        except SystemExit as e:
            notes.append(f"{tld}: {e}")
            print(f"{tld}: zone step failed (see notes)")
            continue
        zstats[tld] = {"count": r["count"], "removed": r["removed"],
                       "added": r["added"], "first_run": r["first_run"]}
        print(f"{tld}: {r['count']:,} names, "
              f"-{r['removed']:,} / +{r['added']:,}"
              + (" (baseline day)" if r["first_run"] else ""))
        entries = ledger.load(tld, backup_dir)
        ledger.update(tld, entries, r["removed_path"], r["added_path"],
                      cfg, namerules.name_ok, today, log)
        all_entries[tld] = entries
        if not r["first_run"]:
            any_diff = True

    sstats: dict = {}
    if all_entries:
        sstats.update(screen.opr_stage(all_entries, cfg, log))
        sstats.update(screen.vet_stage(all_entries, cfg, log))
        new_alerts_start = len(available)
        sstats.update(screen.drop_stage(all_entries, cfg, log,
                                        alerted, available))
        for row in available[new_alerts_start:]:
            alerts_hist.append({"domain": row["domain"], "date": row["found"],
                                "score": row["score"], "kind": "drop"})

    # bootstrap: first run ever, weekly, or forced via env
    bcfg = cfg.get("bootstrap", {})
    boot_due = (os.environ.get("FORCE_BOOTSTRAP") == "1"
                or not os.path.exists(bootstrap.CURSOR)
                or today.weekday() == int(bcfg.get("weekly_dow", 5)))
    if bcfg.get("enabled", True) and boot_due:
        baselines = {t: os.path.join(zones.CACHE_DIR, f"{t}.names.gz")
                     for t in cfg["tlds"]}
        try:
            before = len(available)
            bs = bootstrap.run(cfg, baselines, log, alerted, available)
            sstats.update(bs)
            for row in available[before:]:
                alerts_hist.append({"domain": row["domain"],
                                    "date": row["found"],
                                    "score": row["score"],
                                    "kind": "bootstrap"})
            print(f"bootstrap: {bs['boot_missing']:,} unregistered "
                  f"majestic names, {bs['boot_found']} passed vetting")
        except SystemExit as e:
            notes.append(f"bootstrap: {e}")
            print("bootstrap step failed (see notes)")
        except Exception:
            notes.append("bootstrap: " + traceback.format_exc(limit=1))
            print("bootstrap step crashed (see notes)")

    # persist ledgers + weekly private backup
    for tld, entries in all_entries.items():
        ledger.save(tld, entries)
    if today.weekday() == 6 and all_entries:
        os.makedirs(backup_dir, exist_ok=True)
        for tld in all_entries:
            src = os.path.join(ledger.LEDGER_DIR, f"{tld}.csv.gz")
            if os.path.exists(src):
                shutil.copyfile(src, os.path.join(backup_dir,
                                                  f"{tld}.csv.gz"))

    # dashboard feed
    seen: set[str] = set()
    dedup = []
    for row in reversed(available):              # newest copy wins
        if row["domain"] in seen:
            continue
        seen.add(row["domain"])
        dedup.append(row)
    available = list(reversed(dedup))[-RESULTS_KEEP_AVAILABLE:]

    results = {
        "generated": dt.datetime.now(dt.timezone.utc)
                       .strftime("%Y-%m-%dT%H:%M:%S") + "Z",
        "run": {"seconds": int(time.time() - t0), "notes": notes,
                "ok": not notes},
        "zones": zstats,
        "ledger": {t: ledger.counts(e) for t, e in all_entries.items()},
        "screening": sstats,
        "available_now": available,
        "upcoming": screen.upcoming(all_entries, cfg) if all_entries else [],
        "alerts": alerts_hist[-RESULTS_KEEP_ALERTS:],
        "config_echo": {
            "alert_min_score": cfg["scoring"]["alert_min_score"],
            "tlds": cfg["tlds"],
        },
    }
    with open(os.path.join(state_dir, "results.json"), "w",
              encoding="utf-8") as f:
        json.dump(results, f, indent=1)
    with open(os.path.join(state_dir, "alerted.json"), "w",
              encoding="utf-8") as f:
        json.dump(alerted, f, indent=1)

    print(f"done in {int(time.time() - t0)}s; "
          f"watching {sum(ledger.counts(e).get('watch', 0) for e in all_entries.values())}, "
          f"available list {len(available)}, notes {len(notes)}")
    if notes and not any_diff and not zstats:
        raise SystemExit("every zone step failed")


if __name__ == "__main__":
    run()
