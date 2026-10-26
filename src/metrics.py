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

# Last error per metric, so callers can log API failures instead of
# mistaking them for "every domain scored zero".
LAST_ERROR: dict[str, str] = {}


def _get(url: str, headers: dict | None = None, timeout: int = 25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def ahrefs_dr(domain: str) -> float | None:
    """Domain Rating by Ahrefs via their free public endpoint.

    Docs: https://docs.ahrefs.com/api/reference/public/post-domain-rating-free
    POST https://api.ahrefs.com/v3/public/domain-rating-free
    body {"targets": [...]}; needs a free-account APIv3 key as Bearer.
    """
    key = os.environ.get("AHREFS_API_KEY")
    if not key:
        LAST_ERROR["ahrefs"] = "AHREFS_API_KEY not set"
        return None
    body = json.dumps({"targets": [domain]}).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                "https://api.ahrefs.com/v3/public/domain-rating-free?output=json",
                data=body,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json",
                         "User-Agent": UA},
            )
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            targets = (data.get("domain_rating") or {}).get("targets") or []
            if targets:
                val = targets[0].get("domain_rating")
                LAST_ERROR.pop("ahrefs", None)
                return float(val) if val is not None else None
            LAST_ERROR["ahrefs"] = "empty targets in response"
            return None
        except urllib.error.HTTPError as e:
            if e.code == 429:           # rate limited: back off and retry
                time.sleep(20 * (attempt + 1))
                continue
            LAST_ERROR["ahrefs"] = f"HTTP {e.code}"
            return None
        except Exception as e:
            LAST_ERROR["ahrefs"] = str(e)[:80]
            return None
    LAST_ERROR["ahrefs"] = "HTTP 429 after retries"
    return None


def open_pagerank(domains: list[str]) -> dict[str, dict]:
    """Open PageRank (openpagerank.com), free API.
    GET https://openpagerank.com/api/v1.0/getPageRank?domains[]=a.com&...
    header API-OPR: <key>; up to 100 domains per call.
    Returns {domain: {"opr": 0..10, "ref_domains": None}}.
    """
    key = os.environ.get("OPR_API_KEY")
    out: dict[str, dict] = {}
    if not domains:
        return out
    if not key:
        LAST_ERROR["opr"] = "OPR_API_KEY not set"
        return out
    for i in range(0, len(domains), 100):
        batch = domains[i:i + 100]
        qs = urllib.parse.urlencode([("domains[]", d) for d in batch])
        req = urllib.request.Request(
            "https://openpagerank.com/api/v1.0/getPageRank?" + qs,
            headers={"API-OPR": key, "User-Agent": UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            for row in data.get("response", []):
                d = (row.get("domain") or "").lower()
                if d:
                    out[d] = {
                        "opr": float(row.get("page_rank_decimal") or 0),
                        "ref_domains": None,
                    }
            LAST_ERROR.pop("opr", None)
        except urllib.error.HTTPError as e:
            LAST_ERROR["opr"] = f"HTTP {e.code}"
        except Exception as e:
            LAST_ERROR["opr"] = str(e)[:80]
        time.sleep(1)
    return out


DATASET_URL = ("https://openpagerank.keywordseverywhere.com/"
               "downloads/top10milliondomains.csv.zip")
DATASET_MAX_AGE_DAYS = 35
DATASET_MIN_ROWS = 5_000_000


def ensure_opr_dataset(cache_dir: str = "zonecache",
                       log=lambda m: None) -> str | None:
    """Monthly top-10M Open PageRank dataset as a sorted 'domain<TAB>opr'
    file. Returns its path, or None if it cannot be fetched (callers then
    fall back to the API). Zero quota, no key, no IP binding."""
    import subprocess
    import tempfile
    import zipfile
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, "opr.tsv")
    if os.path.exists(path):
        age = (time.time() - os.path.getmtime(path)) / 86400
        if age < DATASET_MAX_AGE_DAYS:
            return path
    tmpd = tempfile.mkdtemp(dir=cache_dir)
    zpath = os.path.join(tmpd, "opr.zip")
    try:
        req = urllib.request.Request(DATASET_URL, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=900) as r, \
                open(zpath, "wb") as out:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        import csv as _csv
        import io as _io
        raw = os.path.join(tmpd, "opr.raw")
        rows = 0
        with zipfile.ZipFile(zpath) as z:
            member = next(n for n in z.namelist()
                          if n.lower().endswith(".csv"))
            with z.open(member) as f, open(raw, "w", encoding="utf-8") as out:
                reader = _csv.reader(
                    _io.TextIOWrapper(f, "utf-8", errors="replace"))
                header = next(reader)
                low = [h.strip().lower() for h in header]
                di = next(i for i, h in enumerate(low) if h == "domain")
                oi = next(i for i, h in enumerate(low) if "page rank" in h)
                for row in reader:
                    try:
                        d = row[di].strip().lower()
                        v = float(row[oi])
                    except (IndexError, ValueError):
                        continue
                    if d:
                        out.write(f"{d}\t{v:g}\n")
                        rows += 1
        if rows < DATASET_MIN_ROWS:
            raise RuntimeError(f"dataset looks truncated ({rows:,} rows)")
        env = dict(os.environ, LC_ALL="C")
        subprocess.run(["sort", "-t", "\t", "-k", "1,1", "-S", "25%",
                        "-T", tmpd, "-o", path + ".new", raw],
                       check=True, env=env)
        os.replace(path + ".new", path)
        log(f"opr dataset refreshed: {rows:,} rows")
        LAST_ERROR.pop("opr_dataset", None)
        return path
    except Exception as e:
        LAST_ERROR["opr_dataset"] = str(e)[:100]
        log(f"opr dataset fetch failed: {str(e)[:100]}")
        # a stale copy beats the API fallback's daily cap
        return path if os.path.exists(path) else None
    finally:
        import shutil as _sh
        _sh.rmtree(tmpd, ignore_errors=True)


def opr_dataset_lookup(path: str, domains: list[str]) -> dict[str, dict]:
    """Join the queue against the sorted dataset. Domains absent from the
    top-10M get no row (treated as opr 0 by the caller)."""
    import subprocess
    import tempfile
    out: dict[str, dict] = {}
    if not domains:
        return out
    with tempfile.NamedTemporaryFile("w", suffix=".q", delete=False,
                                     encoding="utf-8") as f:
        qpath = f.name
        for d in sorted(set(x.lower() for x in domains)):
            f.write(d + "\n")
    try:
        env = dict(os.environ, LC_ALL="C")
        res = subprocess.run(
            ["join", "-t", "\t", "-1", "1", "-2", "1", qpath, path],
            check=True, capture_output=True, text=True, env=env)
        for line in res.stdout.splitlines():
            try:
                d, v = line.split("\t", 1)
                out[d] = {"opr": float(v), "ref_domains": None}
            except ValueError:
                continue
    finally:
        os.unlink(qpath)
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
