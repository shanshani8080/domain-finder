"""Authority and index metrics from free, official APIs.

All functions degrade gracefully: if a key is missing or a request fails,
they return None and the caller treats the metric as "unknown" rather than
failing the whole run.

Keys are read from environment variables (set as GitHub Actions secrets):
  AHREFS_API_KEY        free APIv3 key from a free Ahrefs account
  OPR_API_KEY           Open PageRank key (openpagerank.com, free)
  MOZ_ACCESS_ID / MOZ_SECRET_KEY   optional, Moz Links API free tier
  GOOGLE_CSE_KEY / GOOGLE_CSE_CX   optional, Custom Search JSON API (100/day free)
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
import urllib.request

UA = "domain-finder/1.0 (personal research tool)"


def _get(url: str, headers: dict | None = None, timeout: int = 25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def ahrefs_dr(domain: str) -> float | None:
    """Domain Rating by Ahrefs, via their free public endpoint.

    Docs: https://docs.ahrefs.com/en/api/reference/public/get-domain-rating-free
    Free requests; needs a (free-account) APIv3 key as a Bearer token.
    """
    key = os.environ.get("AHREFS_API_KEY")
    if not key:
        return None
    url = ("https://api.ahrefs.com/v3/public/domain-rating-free?"
           + urllib.parse.urlencode({"target": domain, "output": "json"}))
    for attempt in range(3):
        try:
            data = _get(url, headers={"Authorization": f"Bearer {key}"})
            dr = data.get("domain_rating", {})
            val = dr.get("domain_rating") if isinstance(dr, dict) else dr
            return float(val) if val is not None else None
        except urllib.error.HTTPError as e:
            if e.code == 429:           # rate limited: back off and retry
                time.sleep(20 * (attempt + 1))
                continue
            return None
        except Exception:
            return None
    return None


def open_pagerank(domains: list[str]) -> dict[str, dict]:
    """Open Page Rank (now run by Keywords Everywhere).
    Endpoint: POST https://openpagerank.keywordseverywhere.com/v1/domains/bulk
    Free tier: 30,000 domains/month, up to 100 per call, Bearer key.
    Returns {domain: {"opr": 0..10, "ref_domains": int}}.
    """
    key = os.environ.get("OPR_API_KEY")
    out: dict[str, dict] = {}
    if not key or not domains:
        return out
    for i in range(0, len(domains), 100):
        batch = domains[i:i + 100]
        body = json.dumps({"domains": batch, "include_history": False}).encode()
        req = urllib.request.Request(
            "https://openpagerank.keywordseverywhere.com/v1/domains/bulk",
            data=body,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "User-Agent": UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            for row in data.get("results", []):
                d = (row.get("domain") or "").lower()
                if d:
                    out[d] = {
                        "opr": float(row.get("open_page_rank") or 0),
                        "ref_domains": int(row.get("referring_domains") or 0),
                    }
        except Exception:
            pass
        time.sleep(1)
    return out


def moz_da(domain: str) -> float | None:
    """Moz Domain Authority via the Links API (optional free tier)."""
    aid = os.environ.get("MOZ_ACCESS_ID")
    sec = os.environ.get("MOZ_SECRET_KEY")
    if not aid or not sec:
        return None
    try:
        token = base64.b64encode(f"{aid}:{sec}".encode()).decode()
        body = json.dumps({"targets": [domain]}).encode()
        req = urllib.request.Request(
            "https://lsapi.seomoz.com/v2/url_metrics",
            data=body,
            headers={"Authorization": f"Basic {token}",
                     "Content-Type": "application/json",
                     "User-Agent": UA},
        )
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode())
        results = data.get("results") or []
        if results:
            return float(results[0].get("domain_authority") or 0)
    except Exception:
        return None
    return None


def google_indexed(domain: str) -> int | None:
    """Approximate count of pages Google has indexed for the domain,
    via the Custom Search JSON API (free quota: 100 queries/day).

    Returns the estimated total results for `site:domain`, or None if the
    check is not configured / failed. For a dropped domain this is usually
    0, which is fine; a leftover index count is a bonus signal.
    """
    key = os.environ.get("GOOGLE_CSE_KEY")
    cx = os.environ.get("GOOGLE_CSE_CX")
    if not key or not cx:
        return None
    url = ("https://www.googleapis.com/customsearch/v1?"
           + urllib.parse.urlencode({"key": key, "cx": cx, "q": f"site:{domain}"}))
    try:
        data = _get(url)
        info = data.get("searchInformation", {})
        return int(info.get("totalResults", "0"))
    except Exception:
        return None
