"""Majestic-minus-zone: quality names that are NOT registered right now.

The Majestic Million lists the most-linked domains on the web. Any entry
absent from today's zone file is a linked domain sitting unregistered:
available at hand-reg price today. This pass finds them, confirms with
RDAP, vets the best, emails winners, and feeds the dashboard. It runs on
the first day and weekly after that (plus on demand).

Budgets keep it inside free quotas; leftovers carry to the next weekly
pass via the bootstrap cursor kept with the ledger cache.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import contextlib
import os
import subprocess
import urllib.request

from . import metrics, namerules, rdap, vet
from .emailer import send_alert

MAJESTIC_URL = "https://downloads.majestic.com/majestic_million.csv"
CURSOR = "ledger/bootstrap_done.txt"       # names already fully processed


def _majestic_names(cfg: dict) -> list[str]:
    req = urllib.request.Request(MAJESTIC_URL,
                                 headers={"User-Agent": zones_ua()})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read().decode("utf-8", "replace")
    reader = csv.DictReader(io.StringIO(data))
    if "Domain" not in (reader.fieldnames or []):
        raise SystemExit("majestic download did not return the expected CSV")
    out = []
    for row in reader:
        d = (row.get("Domain") or "").strip().lower()
        if d and namerules.name_ok(d, cfg):
            out.append(d)
    if len(out) < 5_000:
        raise SystemExit(f"majestic data looks truncated ({len(out)} usable)")
    return out


def zones_ua() -> str:
    from .zones import UA
    return UA


def _not_in_zones(names: list[str], baselines: dict[str, str],
                  work_dir: str) -> list[str]:
    """names (name-ok, any of our tlds) minus the union of zone baselines."""
    os.makedirs(work_dir, exist_ok=True)
    cand = os.path.join(work_dir, "boot.cand")
    with open(cand, "w", encoding="utf-8") as f:
        for d in sorted(set(names)):
            f.write(d + "\n")
    missing: list[str] = []
    env = dict(os.environ, LC_ALL="C")
    remaining = cand
    for tld, gz in baselines.items():
        if not os.path.exists(gz):
            continue
        nxt = os.path.join(work_dir, f"boot.minus.{tld}")
        subprocess.run(
            ["bash", "-c",
             f"set -o pipefail; comm -23 {remaining} <(zcat {gz}) > {nxt}"],
            check=True, env=env)
        remaining = nxt
    with open(remaining, encoding="utf-8") as f:
        for line in f:
            d = line.strip()
            if d:
                missing.append(d)
    return missing


def _done_set() -> set[str]:
    if not os.path.exists(CURSOR):
        return set()
    with open(CURSOR, encoding="utf-8") as f:
        return {l.strip() for l in f if l.strip()}


def _mark_done(names: list[str]) -> None:
    os.makedirs(os.path.dirname(CURSOR), exist_ok=True)
    with open(CURSOR, "a", encoding="utf-8") as f:
        for d in names:
            f.write(d + "\n")


def run(cfg: dict, baselines: dict[str, str], log, alerted: dict,
        available: list, work_dir: str = "ztmp") -> dict:
    bcfg = cfg.get("bootstrap", {})
    rdap_budget = int(bcfg.get("rdap_budget", 300))
    vet_budget = int(bcfg.get("vet_budget", 40))
    bar = int(cfg["scoring"]["alert_min_score"])
    cap = int(cfg["scoring"]["max_alerts_per_day"])

    names = _majestic_names(cfg)
    missing = _not_in_zones(names, baselines, work_dir)
    log(f"bootstrap: {len(names):,} majestic name-ok, "
        f"{len(missing):,} absent from zones")

    done = _done_set()
    queue = [d for d in missing if d not in done and d not in alerted]
    queue.sort(key=lambda d: -namerules.name_quality(d))

    stats = {"boot_missing": len(missing), "boot_rdap": 0, "boot_vetted": 0,
             "boot_found": 0, "boot_alerts": 0}
    processed: list[str] = []
    vetted = 0
    sent = 0
    for d in queue:
        if stats["boot_rdap"] >= rdap_budget or vetted >= vet_budget:
            break
        info = rdap.lookup(d)
        stats["boot_rdap"] += 1
        if info.get("found"):
            processed.append(d)            # registered after all; skip forever
            continue
        with contextlib.redirect_stdout(io.StringIO()):
            v = vet.vet_domain(d, cfg)
        vetted += 1
        stats["boot_vetted"] += 1
        processed.append(d)
        if v.get("disqualified"):
            log(f"bootstrap vet fail: {d} ({v['disqualified']})")
            continue
        dr = metrics.ahrefs_dr(d)
        ds = metrics.ensure_opr_dataset(log=log)
        if ds:
            opr_row = metrics.opr_dataset_lookup(ds, [d]).get(d) or {}
        else:
            opr_row = metrics.open_pagerank([d]).get(d) or {}
        opr = opr_row.get("opr")
        authority = (dr / 10.0) if dr is not None else (opr or 0)
        score = vet.score(v, authority, cfg)
        hist = v.get("history") or {}
        if score < bar:
            log(f"bootstrap low: {d} score={score}")
            continue
        from .screen import _authority_ok
        if not _authority_ok(dr, opr, cfg):
            log(f"bootstrap no authority: {d} score={score} dr={dr} "
                f"opr={opr}; not alert-worthy")
            continue
        stats["boot_found"] += 1
        row = {"domain": d, "source": "bootstrap", "score": score,
               "dr": dr, "opr": opr,
               "ref_domains": opr_row.get("ref_domains"),
               "first_year": str(hist.get("first_year", "")),
               "gone": None, "found": dt.date.today().isoformat()}
        available.append(row)
        log(f"BOOTSTRAP FIND: {d} score={score} dr={dr} opr={opr}")
        item = dict(v)
        item.update({"score": score, "ahrefs_dr": dr, "open_pagerank": opr,
                     "ref_domains": opr_row.get("ref_domains"),
                     "status": "AVAILABLE NOW - register it",
                     "drop_date": None,
                     "_ahrefs_link_base": cfg.get("alerts", {})
                                             .get("ahrefs_link_base")})
        if sent < cap and send_alert(item, cfg):
            sent += 1
            alerted[d] = dt.date.today().isoformat()
    stats["boot_alerts"] = sent
    _mark_done(processed)
    return stats
