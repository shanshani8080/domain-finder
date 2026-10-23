"""Staged screening of ledger entries, inside free-API budgets.

Order per day (all budgets from the private config):
  1. Open PageRank in bulk on 'new' entries (900/day default, 100 per call):
     any real PageRank -> 'pass', else 'oprfail'.
  2. Wayback vet (CDX + era-sampled snapshots) on 'pass' (120/day default):
     spam phase / thin history -> 'vetfail'.
  3. Ahrefs DR on vetted keepers (40/day default), final 0-100 score:
     >= alert_min_score -> 'watch', else 'low'.
  4. RDAP on 'watch' entries old enough to drop (400/day default):
     registry 404 -> AVAILABLE -> email + 'alerted'.
     'held' entries (overdue) are re-checked weekly.

Public logs show counts only; domain-level detail goes to the private
run log and results feed.
"""
from __future__ import annotations

import datetime as dt
import io
import contextlib

from . import metrics, namerules, rdap, vet
from .emailer import send_alert


def _ledger_cfg(cfg: dict) -> dict:
    return cfg.get("ledger", {})


def _queue(entries: dict[str, dict], stage: str) -> list[str]:
    q = [d for d, e in entries.items() if e.get("stage") == stage]
    q.sort(key=lambda d: (entries[d].get("gone", ""),
                          -namerules.name_quality(d)))
    return q


def opr_stage(all_entries: dict[str, dict[str, dict]], cfg: dict, log) -> dict:
    budget = int(_ledger_cfg(cfg).get("opr_budget_per_day", 900))
    opr_min = float(_ledger_cfg(cfg).get("opr_min", 1.0))
    picked: list[tuple[str, str]] = []           # (tld, domain)
    for tld, entries in all_entries.items():
        for d in _queue(entries, "new"):
            picked.append((tld, d))
    picked.sort(key=lambda td: (all_entries[td[0]][td[1]].get("gone", ""),
                                -namerules.name_quality(td[1])))
    picked = picked[:budget]
    checked = 0
    passed = 0
    doms = [d for _, d in picked]
    scores = metrics.open_pagerank(doms)
    iso = dt.date.today().isoformat()
    for tld, d in picked:
        e = all_entries[tld][d]
        row = scores.get(d) or {}
        opr = float(row.get("opr") or 0)
        e["opr"] = f"{opr:g}"
        e["ref"] = str(row.get("ref_domains") or "")
        e["last"] = iso
        checked += 1
        if opr >= opr_min:
            e["stage"] = "pass"
            passed += 1
            log(f"opr pass: {d} opr={opr:g} ref={e['ref']}")
        else:
            e["stage"] = "oprfail"
    return {"opr_checked": checked, "opr_passed": passed}


def vet_stage(all_entries: dict[str, dict[str, dict]], cfg: dict, log) -> dict:
    budget = int(_ledger_cfg(cfg).get("cdx_budget_per_day", 120))
    dr_budget = int(_ledger_cfg(cfg).get("dr_budget_per_day", 40))
    bar = int(cfg["scoring"]["alert_min_score"])
    iso = dt.date.today().isoformat()
    vetted = 0
    kept = 0
    dr_used = 0
    picked: list[tuple[str, str]] = []
    for tld, entries in all_entries.items():
        for d in _queue(entries, "pass"):
            picked.append((tld, d))
    picked.sort(key=lambda td: float(all_entries[td[0]][td[1]].get("opr") or 0),
                reverse=True)
    for tld, d in picked[:budget]:
        e = all_entries[tld][d]
        e["last"] = iso
        with contextlib.redirect_stdout(io.StringIO()):
            v = vet.vet_domain(d, cfg)
        vetted += 1
        if v.get("disqualified"):
            e["stage"] = "vetfail"
            log(f"vet fail: {d} ({v['disqualified']})")
            continue
        hist = v.get("history") or {}
        e["first_year"] = str(hist.get("first_year", ""))
        opr = float(e.get("opr") or 0)
        dr = None
        if dr_used < dr_budget:
            dr = metrics.ahrefs_dr(d)
            dr_used += 1
        authority = (dr / 10.0) if dr is not None else opr
        score = vet.score(v, authority, cfg)
        e["score"] = str(score)
        e["dr"] = "" if dr is None else f"{dr:g}"
        if score >= bar:
            e["stage"] = "watch"
            kept += 1
            log(f"WATCH: {d} score={score} dr={dr} opr={opr:g} "
                f"since={e['first_year']}")
        else:
            e["stage"] = "low"
            log(f"low: {d} score={score} (bar {bar})")
    return {"vetted": vetted, "vet_passed": kept, "dr_checked": dr_used}


def drop_stage(all_entries: dict[str, dict[str, dict]], cfg: dict, log,
               alerted: dict, available: list) -> dict:
    lcfg = _ledger_cfg(cfg)
    budget = int(lcfg.get("rdap_budget_per_day", 400))
    watch_age = int(lcfg.get("watch_age_days", 25))
    max_watch = int(lcfg.get("max_watch_days", 85))
    cap = int(cfg["scoring"]["max_alerts_per_day"])
    today = dt.date.today()
    iso = today.isoformat()
    weekly = today.weekday() == 6
    checked = 0
    sent = 0
    todo: list[tuple[str, dict]] = []
    for tld, entries in all_entries.items():
        for d, e in sorted(entries.items()):
            todo.append((d, e))
    for d, e in todo:
            if checked >= budget:
                break
            stage = e.get("stage")
            if stage not in ("watch", "held"):
                continue
            try:
                age = (today - dt.date.fromisoformat(e["gone"])).days
            except (KeyError, ValueError):
                age = 0
            if stage == "watch" and age < watch_age:
                continue
            if stage == "held" and not weekly:
                continue
            info = rdap.lookup(d)
            checked += 1
            e["last"] = iso
            if not info.get("found"):
                # the drop happened: it is registerable right now
                item = _final_item(d, e, cfg, "RELEASED - register it now")
                e["stage"] = "alerted"
                alerted[d] = iso
                available.append(_dash_row(d, e, "drop"))
                log(f"AVAILABLE: {d} score={e.get('score')} dr={e.get('dr')}")
                if sent < cap and send_alert(item, cfg):
                    sent += 1
            elif stage == "watch" and age > max_watch:
                e["stage"] = "held"
    return {"rdap_checked": checked, "alerts_sent": sent}


def _final_item(d: str, e: dict, cfg: dict, status: str) -> dict:
    item = {
        "domain": d, "status": status, "drop_date": None,
        "score": int(e.get("score") or 0),
        "ahrefs_dr": float(e["dr"]) if e.get("dr") else None,
        "open_pagerank": float(e["opr"]) if e.get("opr") else None,
        "ref_domains": int(e["ref"]) if e.get("ref") else None,
        "history": {"first_year": e.get("first_year") or "?"},
        "snapshots": [],
        "_ahrefs_link_base": cfg.get("alerts", {}).get("ahrefs_link_base"),
    }
    return item


def _dash_row(d: str, e: dict, source: str) -> dict:
    return {
        "domain": d, "source": source,
        "score": int(e.get("score") or 0),
        "dr": float(e["dr"]) if e.get("dr") else None,
        "opr": float(e["opr"]) if e.get("opr") else None,
        "ref_domains": int(e["ref"]) if e.get("ref") else None,
        "first_year": e.get("first_year") or None,
        "gone": e.get("gone"),
        "found": dt.date.today().isoformat(),
    }


def upcoming(all_entries: dict[str, dict[str, dict]], cfg: dict,
             limit: int = 40) -> list[dict]:
    """Watched names expected to drop soon, best first."""
    watch_age = int(_ledger_cfg(cfg).get("watch_age_days", 25))
    rows = []
    for tld, entries in all_entries.items():
        for d, e in entries.items():
            if e.get("stage") not in ("watch", "held"):
                continue
            try:
                gone = dt.date.fromisoformat(e["gone"])
            except (KeyError, ValueError):
                continue
            eta = gone + dt.timedelta(days=max(watch_age + 10, 35))
            rows.append({
                "domain": d, "days_watched": (dt.date.today() - gone).days,
                "score": int(e.get("score") or 0),
                "dr": float(e["dr"]) if e.get("dr") else None,
                "opr": float(e["opr"]) if e.get("opr") else None,
                "first_year": e.get("first_year") or None,
                "gone": e["gone"], "eta": eta.isoformat(),
                "stage": e.get("stage"),
            })
    rows.sort(key=lambda r: (-r["score"], r["eta"]))
    return rows[:limit]
