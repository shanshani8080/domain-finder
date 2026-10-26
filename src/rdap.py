"""RDAP lookups (registry registration status). rdap.org routes each query
to the right registry. HTTP 404 means the name is NOT registered."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

UA = "domain-finder/2.0 (personal research tool)"


def lookup(domain: str, timeout: int = 15) -> dict:
    url = f"https://rdap.org/domain/{domain}"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": "application/rdap+json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        statuses = [s.lower() for s in data.get("status", [])]
        exp = next((e.get("eventDate", "")[:10] for e in data.get("events", [])
                    if e.get("eventAction") == "expiration"), None)
        return {"found": True, "statuses": statuses, "expiration": exp}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"found": False}          # released: can be hand-registered
        return {"found": True, "statuses": [], "error": e.code}
    except Exception as e:
        return {"found": True, "statuses": [], "error": str(e)[:60]}
