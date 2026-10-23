"""Domain-name quality rules, shared by every stage."""
from __future__ import annotations

import re

from . import lexicons


def name_ok(domain: str, cfg: dict) -> bool:
    """Hard gate: tld allowed, length window, letters only, no excluded terms."""
    try:
        name, tld = domain.rsplit(".", 1)
    except ValueError:
        return False
    rules = cfg["name_rules"]
    if tld not in cfg["tlds"]:
        return False
    if not (rules["min_length"] <= len(name) <= rules["max_length"]):
        return False
    if rules.get("letters_only", True) and not re.fullmatch(r"[a-z]+", name):
        return False
    if lexicons.name_is_disqualified(name):
        return False
    return True


_VOWELS = set("aeiou")


def name_quality(domain: str) -> float:
    """0..1 heuristic used ONLY to order the screening queue when the daily
    backlog exceeds the free-API budget. Shorter, pronounceable names first."""
    name = domain.split(".", 1)[0]
    n = len(name)
    length_score = max(0.0, 1.0 - (n - 4) / 14)          # 4 chars=1.0, 18=0.0
    vowels = sum(1 for c in name if c in _VOWELS)
    ratio = vowels / n if n else 0
    pron = 1.0 - min(1.0, abs(ratio - 0.4) * 2.5)        # ~40% vowels reads best
    runs = max((len(m.group(0)) for m in re.finditer(r"[^aeiou]+", name)), default=0)
    cluster = 1.0 if runs <= 3 else max(0.0, 1.0 - (runs - 3) * 0.25)
    return round(0.45 * length_score + 0.35 * pron + 0.20 * cluster, 3)
