"""Word lists used across the pipeline.

Three kinds of lists:
- HARD_DQ_NAME: if any of these appear inside the domain NAME, the domain is
  excluded permanently (owner content rules + obvious spam niches).
- SPAM_CONTENT: if these appear in archived PAGE CONTENT, that snapshot is a
  spam phase (casino/adult/pharma/replica in several languages).
- PARKED: fingerprints of parked / for-sale / dead placeholder pages.
- TRADEMARKS: well known live brands; a domain whose name contains one is
  skipped to avoid legal trouble and confusion.

All matching is lowercase substring unless noted.
"""

# ---- excluded niches + junk, applied to the domain name itself ----
HARD_DQ_NAME = [
    # gambling / betting
    "casino", "bet", "poker", "slot", "gambl", "jackpot", "roulette",
    "bingo", "lotto", "lottery", "wager", "bookmaker", "toto", "bahis",
    "judi", "togel", "casib", "giris", "bonusu", "kumar",
    # adult
    "porn", "sex", "xxx", "adult", "escort", "milf", "nude", "naked",
    "cam4", "camgirl", "hentai", "slut", "fetish", "bdsm",
    # alcohol
    "beer", "wine", "whisky", "whiskey", "vodka", "brewery", "brewing",
    "liquor", "booze", "cocktail", "tequila", "champagne", "cigar",
    # pharma / grey
    "viagra", "cialis", "pharma", "steroid", "kamagra", "tramadol",
    # loan/crypto scam-y patterns commonly abused
    "paydayloan", "fastloan", "hyip",
]

# ---- spam vocab scanned inside archived page text ----
SPAM_CONTENT = [
    # english
    "casino", "online casino", "free spins", "sports betting", "poker",
    "slot machine", "jackpot", "escort", "porn", "xxx", "viagra", "cialis",
    "buy backlinks", "pbn network", "link building service", "replica watches",
    "replica handbags", "payday loan",
    # turkish
    "bahis", "casino siteleri", "deneme bonusu", "canli bahis", "giris adresi",
    # korean
    "카지노",          # 카지노 (casino)
    "토토",                # 토토 (toto betting)
    "바카라",          # 바카라 (baccarat)
    # indonesian
    "judi online", "slot gacor", "togel", "situs judi",
    # chinese
    "娱乐城",          # 娱乐城 (casino)
    "彩票",                # 彩票 (lottery)
    "百家乐",          # 百家乐 (baccarat)
    # russian
    "казино",      # казино
    "ставки",      # ставки (bets)
]

# ---- parked / placeholder / for-sale fingerprints ----
PARKED = [
    "related searches", "sponsored listings", "this domain is for sale",
    "buy this domain", "domain is for sale", "make an offer",
    "under construction", "coming soon", "parked free", "sedoparking",
    "parkingcrew", "domain parking", "this web page is parked",
    "renew now", "has expired", "account suspended",
    "future home of something quite cool",
]

# ---- live brands: skip domains whose name contains them ----
TRADEMARKS = [
    "google", "youtube", "facebook", "instagram", "whatsapp", "twitter",
    "tiktok", "amazon", "microsoft", "windows", "office365", "apple",
    "iphone", "ipad", "android", "samsung", "huawei", "xiaomi", "netflix",
    "disney", "marvel", "pokemon", "nintendo", "playstation", "xbox",
    "paypal", "visa", "mastercard", "walmart", "ebay", "alibaba",
    "wordpress", "shopify", "godaddy", "cloudflare", "spotify", "linkedin",
    "reddit", "telegram", "snapchat", "mcafee", "norton", "adobe",
    "photoshop", "tesla", "toyota", "honda", "nike", "adidas", "lego",
    "starbucks", "cocacola", "pepsi", "nfl", "nba", "fifa", "uefa",
    "olympic", "gmail", "outlook", "yahoo", "bing", "chatgpt", "openai",
]


def name_is_disqualified(name_no_tld: str) -> str | None:
    """Return the reason string if the bare name hits a hard rule, else None."""
    n = name_no_tld.lower()
    for term in HARD_DQ_NAME:
        if term in n:
            return f"name contains excluded term '{term}'"
    for brand in TRADEMARKS:
        if brand in n:
            return f"name contains brand '{brand}'"
    return None


def scan_text(text: str) -> dict:
    """Scan a snapshot's text. Returns counts of spam and parked hits."""
    t = text.lower()
    spam_hits = [w for w in SPAM_CONTENT if w in t]
    parked_hits = [w for w in PARKED if w in t]
    return {"spam": spam_hits, "parked": parked_hits}
