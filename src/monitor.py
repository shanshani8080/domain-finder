"""Daily monitor. Designed for a scheduled GitHub Actions run.

Flow:
  1. Take today's slice of the watchlist (full list covered every N days).
  2. DNS-check each domain in the slice (cheap). Trigger when a domain is
     NXDOMAIN, has no NS, or its NS moved to known parking/expiry servers.
  3. RDAP the triggered domains. Keep those in redemptionPeriod/pendingDelete
     (really dying) or already released.
  4. Deep-vet the dying ones (Wayback history + spam scan), pull authority
     metrics (Ahrefs DR free endpoint, Open PageRank, optional Moz and
     Google index), score, and email only the best.
  5. Re-check previously flagged domains on later runs; the moment RDAP says
     the name is released, send the "available now, register it" email.

State lives in state/state.json and is committed by the workflow, so the
tool never alerts twice for the same domain and never loses its place.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import os
import urllib.request

from . import metrics, vet
from .emailer import send_alert

STATE_PATH = "state/state.json"
WATCHLIST = "state/watchlist.txt"

PARKING_NS = [
    "sedoparking", "parkingcrew", "bodis", "above.com", "parklogic",
    "uniregistry", "dan.com", "afternic", "expirationwarning",
    "pendingrenewaldeletion", "renewyourname", "expired.namebright",
    "justdropped", "snparking", "redirectdom", "suspended-domain",
    "expirypark", "cashparking",
]

DYING_STATUSES = ("redemption period", "pending delete", "redemptionperiod",
                  "pendingdelete")


# ---------------------------------------------------------------- state ----
def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        return json.load(open(STATE_PATH))
    return {"alerted": {}, "watch_drops": {}, "seen_dead": {}}


def save_state(st: dict) -> None:
    os.makedirs("state", exist_ok=True)
    json.dump(st, open(STATE_PATH, "w"), indent=1)


# ------------------------------------------------------------------ dns ----
def ns_lookup(domain: str) -> dict:
    """Resolve NS via DNS-over-HTTPS (Google), so it works from any runner."""
    url = f"https://dns.google/resolve?name={domain}&type=NS"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": vet.UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return {"domain": domain, "state": "error"}
    status = data.get("Status")
    if status == 3:
        return {"domain": domain, "state": "nxdomain"}
    answers = [a.get("data", "").lower() for a in (data.get("Answer") or [])
               if a.get("type") == 2]
    if not answers:
        return {"domain": domain, "state": "no_ns"}
    if any(p in ns for ns in answers for p in PARKING_NS):
        return {"domain": domain, "state": "parking_ns", "ns": answers[:3]}
    return {"domain": domain, "state": "alive"}


def rdap(domain: str, timeout: int = 15) -> dict:
    url = f"https://rdap.org/domain/{domain}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": vet.UA,
                                                   "Accept": "application/rdap+json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        statuses = [s.lower() for s in data.get("status", [])]
        exp = next((e.get("eventDate", "")[:10] for e in data.get("events", [])
                    if e.get("eventAction") == "expiration"), None)
        return {"found": True, "statuses": statuses, "expiration": exp}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"found": False}      # released: can be hand-registered
        return {"found": True, "statuses": [], "error": e.code}
    except Exception as e:
        return {"found": True, "statuses": [], "error": str(e)[:60]}


# ------------------------------------------------------------- pipeline ----
def todays_slice(domains: list[str], slices: int) -> list[str]:
    day = dt.date.today().toordinal() % slices
    return [d for i, d in enumerate(domains) if i % slices == day]


def evaluate(domain: str, cfg: dict, status: str, drop_hint: str | None) -> dict | None:
    """Deep vet + metrics + score. Returns an alert item or None."""
    v = vet.vet_domain(domain, cfg)
    if v.get("disqualified"):
        print(f"  {domain}: DQ ({v['disqualified']})")
        return None

    dr = metrics.ahrefs_dr(domain)
    opr = metrics.open_pagerank([domain]).get(domain)
    moz = metrics.moz_da(domain)
    gidx = metrics.google_indexed(domain) if cfg["metrics"].get("google_index_check") else None

    authority = 0.0
    if dr is not None:
        authority = dr / 10.0
    elif opr is not None:
        authority = opr

    m = cfg["metrics"]
    if dr is not None and dr < m["ahrefs_dr_min"] and (opr or 0) < m["open_pagerank_min"]:
        print(f"  {domain}: authority too low (DR {dr}, OPR {opr})")
        return None
    if dr is None and opr is not None and opr < m["open_pagerank_min"]:
        print(f"  {domain}: OPR too low ({opr})")
        return None

    sc = vet.score(v, authority, cfg)
    print(f"  {domain}: score {sc} (DR {dr}, OPR {opr})")
    if sc < cfg["scoring"]["alert_min_score"]:
        return None

    item = dict(v)
    item.update({"score": sc, "ahrefs_dr": dr, "open_pagerank": opr,
                 "moz_da": moz, "google_indexed": gidx,
                 "status": status, "drop_date": drop_hint})
    return item


def run() -> None:
    cfg = json.load(open("config.json"))
    st = load_state()
    sent_today = 0
    cap = cfg["scoring"]["max_alerts_per_day"]

    # ---- 0. drop-day rechecks for previously flagged names ----
    for d in list(st["watch_drops"].keys()):
        info = rdap(d)
        if not info.get("found"):
            item = st["watch_drops"].pop(d)
            item["status"] = "RELEASED - register it now"
            item["drop_date"] = None
            if send_alert(item, cfg):
                print(f"available-now alert sent for {d}")
            st["alerted"][d] = dt.date.today().isoformat()

    # ---- 1. today's slice ----
    if not os.path.exists(WATCHLIST):
        print("no watchlist yet; run the build-watchlist workflow first")
        save_state(st)
        return
    rows = [l.strip().split(",")[0] for l in open(WATCHLIST) if l.strip()]
    todays = [d for d in todays_slice(rows, cfg["monitor"]["slices"])
              if d not in st["alerted"]]
    print(f"watchlist {len(rows)}; checking {len(todays)} today")

    # ---- 2. DNS fan-out ----
    triggered: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=cfg["monitor"]["dns_concurrency"]) as ex:
        for res in ex.map(ns_lookup, todays):
            if res["state"] in ("nxdomain", "no_ns", "parking_ns"):
                triggered.append(res)
    print(f"dns triggers: {len(triggered)}")

    # ---- 3 + 4. RDAP, vet, score, alert ----
    for t in triggered:
        d = t["domain"]
        if st["seen_dead"].get(d) == t["state"]:
            continue                      # unchanged since last look
        st["seen_dead"][d] = t["state"]
        info = rdap(d)
        if not info.get("found"):
            status, drop = "RELEASED - register it now", None
        elif any(s in DYING_STATUSES for s in info.get("statuses", [])):
            status = ", ".join(info["statuses"][:3])
            drop = info.get("expiration")
        else:
            continue                      # merely parked/held; keep watching

        if sent_today >= cap:
            print("daily alert cap reached; leaving the rest for tomorrow")
            break
        item = evaluate(d, cfg, status, drop)
        if not item:
            continue
        if "RELEASED" in status:
            if send_alert(item, cfg):
                sent_today += 1
                st["alerted"][d] = dt.date.today().isoformat()
        else:
            if send_alert(item, cfg):
                sent_today += 1
            st["watch_drops"][d] = item   # recheck daily until it drops

    save_state(st)
    print(f"done; alerts sent: {sent_today}")


if __name__ == "__main__":
    run()
