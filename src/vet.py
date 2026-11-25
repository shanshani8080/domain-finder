"""Wayback Machine vetting and scoring.

Given a domain, this module answers:
  - how old is it, how rich and continuous is its archive history
  - what was on it across its life (titles per sampled year)
  - did it ever have a spam phase (casino/adult/pharma/replica, several languages)
  - how long has it been parked or dead

Can be used standalone:
    python -m src.vet example.com otherdomain.org
    python -m src.vet --file domains.txt
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

from . import lexicons

UA = "domain-finder/1.0 (personal research tool; contact via registrar)"
CDX = "https://web.archive.org/cdx/search/cdx"
WAYBACK_DELAY = 1.5   # polite spacing between archive.org requests


def _fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def cdx_summary(domain: str) -> dict | None:
    """Monthly-collapsed capture timeline. One cheap request."""
    url = (f"{CDX}?url={urllib.parse.quote(domain)}"
           "&fl=timestamp&collapse=timestamp:6&limit=400")
    text = None
    for attempt in range(2):            # one retry: transient CDX errors
        try:
            text = _fetch(url, timeout=15)
            break
        except Exception:
            if attempt == 0:
                time.sleep(2)
    if text is None:
        return None
    stamps = [l[:6] for l in text.strip().splitlines() if re.match(r"^\d{14}", l)]
    if not stamps:
        return None
    years = sorted({s[:4] for s in stamps})
    first, last = int(years[0]), int(years[-1])
    now = datetime.utcnow().year
    span = max(1, last - first + 1)
    return {
        "first_year": first,
        "last_year": last,
        "age_years": now - first,
        "months_captured": len(stamps),
        "years_covered": len(years),
        "coverage": round(len(years) / span, 2),
        "years_since_last": now - last,
        "year_list": years,
    }


def _pick_sample_years(summary: dict, n: int) -> list[int]:
    """Spread samples across the life, biased to the final two active years
    (where spam phases live)."""
    ys = [int(y) for y in summary["year_list"]]
    if len(ys) <= n:
        return ys
    picks = {ys[0], ys[-1], ys[-2]}
    remaining = [y for y in ys if y not in picks]
    step = max(1, len(remaining) // max(1, (n - len(picks))))
    picks.update(remaining[::step][: n - len(picks)])
    return sorted(picks)[:n]


def fetch_snapshot(domain: str, year: int) -> dict:
    """Fetch the snapshot nearest to mid-`year` and reduce it to signals."""
    url = f"https://web.archive.org/web/{year}0601000000/http://{domain}/"
    try:
        raw = _fetch(url)
    except Exception as e:
        return {"year": year, "ok": False, "error": str(e)[:80]}
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
    title = html.unescape(m.group(1)).strip()[:120] if m else ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text)[:20000]
    hits = lexicons.scan_text(title + " " + text)
    return {
        "year": year,
        "ok": True,
        "title": title,
        "spam_hits": hits["spam"],
        "parked_hits": hits["parked"],
        "empty": len(text.strip()) < 120,
    }


def _title_tokens(t: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", t.lower()) if w not in
            {"the", "and", "for", "com", "net", "org", "www", "home", "page"}}


def consistency_score(snaps: list[dict]) -> float:
    """0..1: do the usable snapshots look like one site with one identity?"""
    titles = [_title_tokens(s["title"]) for s in snaps
              if s.get("ok") and s.get("title") and not s.get("parked_hits")]
    if len(titles) < 2:
        return 0.5
    pairs, sim = 0, 0.0
    for a, b in zip(titles, titles[1:]):
        if a or b:
            pairs += 1
            sim += len(a & b) / max(1, len(a | b))
    return round(sim / pairs, 2) if pairs else 0.5


def vet_domain(domain: str, cfg: dict) -> dict:
    """Full vet. Returns a verdict dict; never raises."""
    out: dict = {"domain": domain, "disqualified": None, "snapshots": []}

    name = domain.rsplit(".", 1)[0]
    dq = lexicons.name_is_disqualified(name)
    if dq:
        out["disqualified"] = dq
        return out

    summary = cdx_summary(domain)
    out["history"] = summary
    if not summary:
        out["disqualified"] = "no wayback history"
        return out

    w = cfg["watchlist"]
    if summary["age_years"] < w["min_first_year_age"]:
        out["disqualified"] = f"too young ({summary['age_years']}y)"
        return out
    if summary["coverage"] < w["min_year_coverage"]:
        out["disqualified"] = f"patchy history (coverage {summary['coverage']})"
        return out

    years = _pick_sample_years(summary, cfg["vetting"]["snapshots_per_domain"])
    for y in years:
        time.sleep(cfg["vetting"].get("wayback_delay_sec", WAYBACK_DELAY))
        snap = fetch_snapshot(domain, y)
        out["snapshots"].append(snap)
        if snap.get("spam_hits"):
            out["disqualified"] = (
                f"spam phase in {y}: {', '.join(snap['spam_hits'][:4])}")
            return out

    usable = [s for s in out["snapshots"] if s.get("ok")]
    parked_years = sum(1 for s in usable if s.get("parked_hits") or s.get("empty"))
    if usable and parked_years / len(usable) > 0.6:
        out["disqualified"] = "parked or empty for most of its archived life"
        return out

    out["consistency"] = consistency_score(out["snapshots"])
    return out


def score(vet: dict, authority_0_10: float | None, cfg: dict) -> int:
    """0..100 using the configured weights. Authority is on a 0..10 scale
    (Ahrefs DR/10, or Open PageRank directly)."""
    if vet.get("disqualified"):
        return 0
    wts = cfg["scoring"]["weights"]
    h = vet["history"]
    s = 0.0
    s += wts["age"] * min(1.0, h["age_years"] / 20)
    s += wts["captures"] * min(1.0, h["months_captured"] / 120)
    s += wts["coverage"] * h["coverage"]
    s += wts["consistency"] * vet.get("consistency", 0.5)
    s += wts["authority"] * (min(1.0, (authority_0_10 or 0) / 10))
    return int(round(s))


def _main(argv: list[str]) -> None:
    cfg = json.load(open("config.json"))
    domains: list[str] = []
    if argv and argv[0] == "--file":
        domains = [l.strip() for l in open(argv[1]) if l.strip()]
    else:
        domains = argv
    for d in domains:
        v = vet_domain(d, cfg)
        print(json.dumps(v, indent=2))


if __name__ == "__main__":
    _main(sys.argv[1:])
