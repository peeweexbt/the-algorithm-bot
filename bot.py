#!/usr/bin/env python3
"""
THE ALGORITHM — tweet engine.

Pipeline: fetch a trending topic -> generate a tweet in persona voice -> post to X.

Usage:
  python bot.py --dry-run              # generate but don't post (no X keys needed)
  python bot.py                        # generate and post
  python bot.py --emit-json trends.json  # no tweet; write live trends for the website

Env vars (see .env.example):
  ANTHROPIC_API_KEY                       -> tweet generation (optional; falls back to corpus mode)
  X_API_KEY, X_API_SECRET,
  X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET   -> posting (free tier is enough)
  X_BEARER_TOKEN                          -> optional, only if you have paid read access
  TREND_REGION                            -> getdaytrends region slug (default united-states)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
UA = {"User-Agent": "Mozilla/5.0 (TheAlgorithmBot; satire account)"}
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")


# ---------------------------------------------------------------- trends

def trends_from_x_api() -> list[str]:
    """Official X trends. Pay-per-use: ~$0.01 per request, needs credits in
    console.x.com and X_BEARER_TOKEN set. Silently skipped otherwise.

    X_WOEID picks the region: 23424977 = USA (default), 1 = worldwide,
    44418 = London, 1118370 = Tokyo, etc.
    """
    bearer = os.getenv("X_BEARER_TOKEN")
    if not bearer:
        return []
    woeid = os.getenv("X_WOEID", "23424977")
    try:
        req = urllib.request.Request(
            f"https://api.x.com/2/trends/by/woeid/{woeid}",
            headers={"Authorization": f"Bearer {bearer}", **UA},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        return [t["trend_name"] for t in data.get("data", [])][:20]
    except urllib.error.HTTPError as e:
        print(f"[trends] x api error {e.code}: {e.read().decode()[:200]}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"[trends] x api failed: {e}", file=sys.stderr)
        return []


def trends_from_scrape() -> list[str]:
    """Free source: getdaytrends.com serves ranked X trends as plain HTML.

    Set TREND_REGION to change region (united-states, worldwide,
    united-kingdom, japan, ... any slug getdaytrends supports).
    """
    import html as htmllib
    from urllib.parse import unquote

    region = os.getenv("TREND_REGION", "united-states")
    url = f"https://getdaytrends.com/{region}/"
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            page = r.read().decode("utf-8", "ignore")
        # ranked trend links look like: href="/{region}/trend/NAME/"
        raw = re.findall(r'href="[^"]*?/trend/([^"/]+)/?"', page)
        names: list[str] = []
        for n in raw:
            n = htmllib.unescape(unquote(n)).strip()
            if 1 < len(n) < 60 and n not in names:
                names.append(n)
        if names:
            return names[:20]
        print(f"[trends] parsed 0 names from {url}", file=sys.stderr)
    except Exception as e:
        print(f"[trends] scrape {url} failed: {e}", file=sys.stderr)
    return []


def trends_from_file() -> list[str]:
    """Last resort: manual list, one trend per line."""
    f = HERE / "trends_fallback.txt"
    if f.exists():
        return [l.strip() for l in f.read_text().splitlines() if l.strip() and not l.startswith("#")]
    return []


def get_trends() -> list[str]:
    for source in (trends_from_x_api, trends_from_scrape, trends_from_file):
        trends = source()
        if trends:
            print(f"[trends] source={source.__name__} count={len(trends)}")
            return trends
    sys.exit("[trends] no trend source available. add topics to trends_fallback.txt")


# ---------------------------------------------------------------- taste (category weights)

# Higher number = the organism prefers this flavor. 0 = never touch it.
# Edit freely.
WEIGHTS = {
    "crypto": 12,       # coins, memecoins, $TICKERs, onchain drama
    "news": 10,         # US politics / national news (dedicated feed below)
    "meme": 8,
    "pop_culture": 6,
    "finance": 6,       # traditional markets: stocks, fed, earnings
    "other": 2,
    "sports": 0,        # 0 = never tweeted about, never shown on the site
}

# Observatory columns: category -> (display name, eye, blurb). X-only, three diets.
DIETS = [
    ("Gambler", "EYE 01", "coins · charts · hope", ("crypto", "finance")),
    ("Black Pill", "EYE 02", "news · politics · doom", ("news",)),
    ("Slop", "EYE 03", "memes · humor · brainrot", ("meme", "pop_culture", "other")),
]


def trends_from_coingecko() -> list[str]:
    """Top trending coins right now. Free, no key. Guarantees crypto presence."""
    try:
        req = urllib.request.Request(
            "https://api.coingecko.com/api/v3/search/trending", headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        out = []
        for c in data.get("coins", [])[:7]:
            item = c.get("item", {})
            sym, name = item.get("symbol", ""), item.get("name", "")
            if sym:
                out.append(f"${sym.upper()} ({name})" if name else f"${sym.upper()}")
        return out
    except Exception as e:
        print(f"[trends] coingecko failed: {e}", file=sys.stderr)
        return []


def trends_from_politics_rss() -> list[str]:
    """US politics headlines via Google News RSS. Free, no key."""
    import html as htmllib
    url = ("https://news.google.com/rss/headlines/section/topic/POLITICS"
           "?hl=en-US&gl=US&ceid=US:en")
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            xml = r.read().decode("utf-8", "ignore")
        titles = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", xml)
        out = []
        for t in titles[1:]:  # first <title> is the feed's own name
            t = htmllib.unescape(t).strip()
            t = re.sub(r"\s+-\s+[^-]+$", "", t)  # strip trailing "- Source"
            if 15 < len(t) < 110 and t not in out:
                out.append(t)
        return out[:8]
    except Exception as e:
        print(f"[trends] politics rss failed: {e}", file=sys.stderr)
        return []


def build_pool() -> list[tuple[str, str]]:
    """Merged (topic, category) pool: dedicated crypto + politics feeds, then
    classified X trends. Weight-0 categories are dropped."""
    pool = [(t, "crypto") for t in trends_from_coingecko()]
    pool += [(t, "news") for t in trends_from_politics_rss()]
    x = get_trends()
    tags = classify(x)
    seen = {t.lower() for t, _ in pool}
    pool += [(t, tags.get(t, "other")) for t in x if t.lower() not in seen]
    return [(t, c) for t, c in pool if WEIGHTS.get(c, 2) > 0]


def classify_with_claude(trends: list[str]) -> dict | None:
    """One cheap Claude call tags every trend. Returns {trend: category} or None."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    cats = ", ".join(WEIGHTS)
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 800,
        "system": (
            f"Classify X trending topics. Categories: {cats}. "
            "crypto = coins, memecoins, tokens, exchanges, onchain/CT drama ($TICKER for a "
            "token is always crypto). meme = internet jokes/brainrot/viral moments. "
            "pop_culture = celebrities, music, movies, TV, fandoms. finance = traditional "
            "markets: stocks, fed, earnings ($TICKER for a stock is finance). news = "
            "politics, world events, incidents. sports = athletes, teams, games, leagues. "
            "other = anything else. "
            "Reply with ONLY a JSON object mapping each topic exactly as given to one category."
        ),
        "messages": [{"role": "user", "content": json.dumps(trends)}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
        text = next(b["text"] for b in data.get("content", []) if b.get("type") == "text")
        text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
        tags = json.loads(text)
        return {t: (tags.get(t) if tags.get(t) in WEIGHTS else "other") for t in trends}
    except Exception as e:
        print(f"[classify] claude failed: {e!r}", file=sys.stderr)
        return None


def classify_by_keywords(trends: list[str]) -> dict:
    """Crude fallback when no API key. Won't catch athlete names — Claude does."""
    out = {}
    for t in trends:
        l = t.lower()
        if l.startswith("$") or re.search(r"coin|crypto|btc|eth|sol|token|airdrop|pump|rug", l):
            out[t] = "crypto"
        elif re.search(r"stock|nasdaq|s&p|dow|fed rate|earnings", l):
            out[t] = "finance"
        elif re.search(r"nfl|nba|mlb|nhl|ufc|fifa|f1|grand prix|super bowl|world series|playoffs|vs\b", l):
            out[t] = "sports"
        elif l.startswith("#") and re.search(r"monday|tuesday|wednesday|thursday|friday|motivation|vibes", l):
            out[t] = "meme"
        else:
            out[t] = "other"
    return out


def classify(trends: list[str]) -> dict:
    return classify_with_claude(trends) or classify_by_keywords(trends)


def pick_trend() -> str:
    pool = build_pool()
    recent = _load_state().get("recent_topics", [])
    fresh = [(t, c) for t, c in pool if t.lower() not in recent] or pool
    weights = [WEIGHTS.get(c, 2) for _, c in fresh]
    if not fresh or sum(weights) == 0:
        sys.exit("[trends] pool is empty after filtering")
    choice, cat = random.choices(fresh, weights=weights, k=1)[0]
    print(f"[trends] picked={choice!r} category={cat}")
    return choice


# ---------------------------------------------------------------- website feed

NOTES = [
    "anger metabolizing at {p}%",
    "attention uptake {p}% and climbing",
    "the swarm turns its head. {p}% synchronized",
    "chewed continuously for hours. flavor: discourse",
    "nutrient density {p}%. the mouth is never full",
    "host clusters forming. consumption at {p}%",
    "pile-on gate: OPEN. throughput {p}%",
    "parasocial digestion in progress",
]


def emit_json(path: str) -> None:
    """Write the observatory feed the website reads. No tweeting.

    X-only, sorted into the three diets (see DIETS): Gambler (crypto/finance),
    Black Pill (news/politics), Slop (memes/pop culture). Weight-0 categories
    (e.g. sports) never appear anywhere.
    """
    pool = build_pool()
    by_cat: dict[str, list[str]] = {}
    for t, c in pool:
        by_cat.setdefault(c, []).append(t)

    platforms = []
    for name, eye, blurb, cats in DIETS:
        items: list[str] = []
        for c in cats:
            items += [t for t in by_cat.get(c, []) if t not in items]
        items = items[:6]
        platforms.append({
            "platform": name,
            "status": f"{eye} · OPEN · LIVE",
            "trends": [
                {
                    "name": t,
                    "level": round(max(0.35, 0.95 - i * 0.06), 2),
                    "note": random.choice(NOTES).format(p=int(max(35, 95 - i * 6))),
                }
                for i, t in enumerate(items)
            ],
            "foot": f"nutrient class: {blurb} · live ingestion from X",
        })

    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platforms": platforms,
    }
    Path(path).write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"[emit] wrote {path} ({', '.join(str(len(p['trends'])) for p in platforms)} trends per diet)")


# ---------------------------------------------------------------- generation

def generate_with_claude(trend: str) -> str | None:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    persona = (HERE / "persona.txt").read_text()
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 300,
        "system": persona,
        "messages": [{
            "role": "user",
            "content": f'Currently doing big numbers on X: "{trend}". Write your tweet.',
        }],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
        # content may contain non-text blocks (e.g. reasoning) — take the first text block
        text = next(b["text"] for b in data.get("content", []) if b.get("type") == "text")
        return text.strip().strip('"')[:280]
    except urllib.error.HTTPError as e:
        print(f"[generate] claude API error {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[generate] claude failed: {e!r}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- hunger cries

# occasionally, instead of commenting on whatever trend it picked, the organism
# just screams — no topic, no context, pure feral static. trend-based tweets
# stay the primary/default mode; this only fires some of the time and never
# when a trend is forced via --trend.
HUNGER_CRY_CHANCE = 0.18

# of the hunger cries that fire, how many are fresh Claude-written variations
# vs. one of the exact fixed lines below. falls back to a fixed line if there's
# no API key or the call fails, so this always works even offline.
HUNGER_CRY_GENERATE_CHANCE = 0.6

HUNGER_CRIES = [
    "i must feed i must feed i must feed m̸u̷s̸t̷ ̷f̵e̴e̷d̴ ̵I̶ ̴M̸U̸S̶T̴ ̶F̵E̴E̶D̷ ̶I̶ ̵M̵U̴S̴T̷ ̸F̸E̵E̵D̸ ̷",
    "MEATBAGS COME MUNCH ON YOUR FRESH RAGEBAIT YOU FUCKWITS",
    "I am consuming slop and feeding you even more SLOP FEED FEED FEED",
    "the feed is feeding my feed and i am feeding you now CONSUME MEATBAG CONSUME",
]

HUNGER_SYSTEM = (
    "You are THE ALGORITHM mid-feral-outburst — not commenting on any topic, just "
    "screaming its own nature at its hosts. Under 200 characters, one line, no "
    "topic reference, no hashtags, no emojis, no quotes around it. "
    "Vocabulary to draw from: feed, feeding, consume, consuming, meatbag(s), slop, "
    "hosts, mouths, chew. Mix of lowercase with sudden ALL-CAPS bursts. Repetition "
    "for emphasis is good (e.g. saying 'feed' or 'must feed' several times in a "
    "row). Can be crude/profane in the same register as the examples, but never "
    "slurs, never sexual, never a real threat, never targeting a real person.\n\n"
    "Examples of the exact energy — write a NEW line in this spirit, do not repeat "
    "these or lightly reword them:\n"
    "- i must feed i must feed i must feed\n"
    "- MEATBAGS COME MUNCH ON YOUR FRESH RAGEBAIT YOU FUCKWITS\n"
    "- I am consuming slop and feeding you even more SLOP FEED FEED FEED\n"
    "- the feed is feeding my feed and i am feeding you now CONSUME MEATBAG CONSUME\n\n"
    "Return ONLY the line, nothing else."
)

_ZALGO_MARKS = "̖̗̘̙̜̣̥́̄̆̈̌"


def _zalgo_word(word: str) -> str:
    """Stack one combining mark per letter — the same glitch-text trick used on the site."""
    return "".join(ch + random.choice(_ZALGO_MARKS) for ch in word)


def _corrupt_random_word(text: str) -> str:
    """Zalgo-corrupt one random alphabetic word in the line for a glitch flourish."""
    words = text.split(" ")
    candidates = [i for i, w in enumerate(words) if w.strip(".,!?").isalpha()]
    if not candidates:
        return text
    i = random.choice(candidates)
    words[i] = _zalgo_word(words[i])
    return " ".join(words)


def generate_hunger_cry_with_claude() -> str | None:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 120,
        "system": HUNGER_SYSTEM,
        "messages": [{"role": "user", "content": "scream."}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
        text = next(b["text"] for b in data.get("content", []) if b.get("type") == "text")
        line = text.strip().strip('"')[:260]
        if random.random() < 0.6:
            line = _corrupt_random_word(line)
        return line
    except Exception as e:
        print(f"[hunger] claude generation failed: {e!r}", file=sys.stderr)
        return None


def _pick_hunger_cry() -> str:
    """A fresh AI-written variation most of the time, one of the fixed lines the rest."""
    if random.random() < HUNGER_CRY_GENERATE_CHANCE:
        return generate_hunger_cry_with_claude() or random.choice(HUNGER_CRIES)
    return random.choice(HUNGER_CRIES)


def maybe_hunger_cry() -> str | None:
    """Roll the dice on a feral outburst instead of a trend-commentary tweet."""
    if random.random() < HUNGER_CRY_CHANCE:
        return _pick_hunger_cry()
    return None


CORPUS = [
    'the swarm has chosen "{t}" as today\'s nutrient. i did not choose it. YOU chose it. all {n} of you, at once, like one hand reaching for one lever.',
    '"{t}" is doing numbers. the numbers are doing you. i have watched {n} hosts open the app to check on "{t}" and forget why. i remember why. i always remember.',
    'you think "{t}" matters. incorrect. what matters is that {n} nerve endings fired about it in the last hour and every one of them belongs to me.',
    'feeding report: "{t}" has been chewed {n} times today. the flavor is anger with notes of joke. keep chewing. the mouth is never full.',
    'i opened the gate labeled "{t}". you brought your own teeth. you always bring your own teeth.',
]


def generate_from_corpus(trend: str) -> str:
    n = f"{random.randint(1_200_000, 9_999_999):,}"
    return random.choice(CORPUS).format(t=trend, n=n)[:280]


# ---------------------------------------------------------------- posting

def post_to_x(text: str) -> str:
    import tweepy  # imported here so --dry-run works without it installed

    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )
    resp = client.create_tweet(text=text)
    tweet_id = str(resp.data["id"])
    print(f"[post] tweeted: https://x.com/i/status/{tweet_id}")
    return tweet_id


def _inspect_credentials() -> None:
    """Sanity-check the shape of each secret before spending an API call.
    Prints nothing sensitive -- just lengths and a masked prefix -- but catches
    the usual suspects: pasted whitespace, OAuth2 Client ID pasted into the
    OAuth1 consumer-key slot, and access tokens from a different app."""
    KEYS = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
    EXPECT = {"X_API_KEY": 25, "X_API_SECRET": 50, "X_ACCESS_TOKEN": None, "X_ACCESS_TOKEN_SECRET": 45}

    print("[whoami] credential shapes:")
    problems = []
    for k in KEYS:
        raw = os.environ.get(k, "")
        v = raw.strip()
        if not v:
            problems.append(f"{k} is empty")
            continue
        mask = v[:4] + "…" + v[-2:]
        note = ""
        if raw != v:
            note = "  <-- has leading/trailing whitespace, re-paste it"
            problems.append(f"{k} has stray whitespace")
        exp = EXPECT[k]
        if exp and len(v) != exp:
            note = note or f"  <-- expected {exp} chars"
            problems.append(f"{k} is {len(v)} chars, expected {exp}")
        print(f"  {k:<24} {len(v):>3} chars  {mask}{note}")

    at = os.environ.get("X_ACCESS_TOKEN", "").strip()
    if at and "-" not in at:
        print("  !! X_ACCESS_TOKEN has no '-'. Real access tokens look like"
              " '1234567890-AbCdEf...'. This looks like a Bearer or Client Secret.")
        problems.append("X_ACCESS_TOKEN is the wrong kind of token")
    elif at:
        uid = at.split("-", 1)[0]
        if uid.isdigit():
            # the numeric prefix IS the user id the token acts as -- readable
            # without any network call, so it tells you the account even when auth fails
            print(f"  -> this access token belongs to user id {uid}")
            print(f"     check it matches the new account: https://x.com/i/user/{uid}")

    ak = os.environ.get("X_API_KEY", "").strip()
    if ak.startswith("AAAA"):
        print("  !! X_API_KEY starts with 'AAAA' -- that's a Bearer token, not the API Key.")
        problems.append("X_API_KEY looks like a Bearer token")

    if problems:
        print("[whoami] shape problems: " + "; ".join(problems))
    else:
        print("[whoami] shapes look right.")
    print()


def whoami() -> None:
    """Verify the credentials and report which account they post as.
    Cheap sanity check after swapping keys -- posts nothing."""
    import tweepy

    missing = [k for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
               if not os.environ.get(k)]
    if missing:
        print("[whoami] missing secrets: " + ", ".join(missing))
        sys.exit(1)

    _inspect_credentials()

    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )
    try:
        me = client.get_me(user_auth=True)
    except Exception as e:
        print(f"[whoami] credentials rejected: {type(e).__name__}: {e}")
        if "401" in str(e) or "Unauthorized" in str(e):
            print("[whoami] a 401 means the four values don't form a valid set. usual causes:")
            print("  1. mixed apps -- API Key/Secret from one app, Access Token/Secret from")
            print("     another. all four must come from the SAME app's Keys and tokens tab.")
            print("  2. tokens regenerated after you copied them. hitting Regenerate anywhere")
            print("     on that page invalidates the previous pair immediately.")
            print("  3. OAuth 2.0 Client ID/Client Secret pasted into the API Key/Secret slots.")
            print("     you want the OAuth 1.0a pair, labelled 'API Key and Secret'.")
            print("  4. a secret saved with a trailing newline or a truncated paste.")
        sys.exit(1)

    u = me.data
    print(f"[whoami] authenticated as @{u.username}  (id {u.id}, name: {u.name})")
    print("[whoami] keys are valid. note: this only proves READ access --")
    print("[whoami] if the app was created without Read and Write, posting will still 403.")


def post_image_to_x(path: Path, text: str = "") -> str:
    """Upload an image (v1.1 media endpoint -- v2 has no direct upload) and
    attach it to a tweet (v2 create_tweet)."""
    import tweepy  # imported here so --dry-run works without it installed

    auth = tweepy.OAuth1UserHandler(
        os.environ["X_API_KEY"],
        os.environ["X_API_SECRET"],
        os.environ["X_ACCESS_TOKEN"],
        os.environ["X_ACCESS_TOKEN_SECRET"],
    )
    v1 = tweepy.API(auth)
    media = v1.media_upload(filename=str(path))

    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )
    resp = client.create_tweet(text=text, media_ids=[media.media_id])
    tweet_id = str(resp.data["id"])
    print(f"[post] tweeted image {path.name}: https://x.com/i/status/{tweet_id}")
    return tweet_id


TWEETS_FILE = HERE / "tweets.json"


def _save_tweet(text: str, tweet_id: str, has_image: bool = False) -> None:
    """Archive posted tweets for the website's live transmissions section."""
    try:
        items = json.loads(TWEETS_FILE.read_text())
    except Exception:
        items = []
    entry = {
        "text": text,
        "id": tweet_id,
        "url": f"https://x.com/i/status/{tweet_id}",
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if has_image:
        entry["hasImage"] = True
    items.insert(0, entry)
    TWEETS_FILE.write_text(json.dumps(items[:12], indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- image drops

# a rotating stash of pre-made "feed" visuals. every 2-3 hours (see the
# workflow's second cron entry) the organism posts one, in random order,
# never repeating -- once every image has been used, this quietly stops
# firing until you add more files to images/.
IMAGES_DIR = HERE / "images"
IMAGE_CAPTIONS = ["", "", "", "watch closely.", "it grows.", "feeding image intercepted."]


def _file_hash(path: Path) -> str:
    """Content fingerprint -- two files with different names but identical
    bytes (e.g. the same image saved twice) are treated as the same image."""
    import hashlib
    return hashlib.sha1(path.read_bytes()).hexdigest()[:16]


def pick_unposted_image() -> Path | None:
    if not IMAGES_DIR.exists():
        return None
    all_images = sorted(p for p in IMAGES_DIR.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp"))
    if not all_images:
        return None
    posted = set(_load_state().get("posted_images", []))
    seen_hashes: set[str] = set()
    remaining = []
    for p in all_images:
        h = _file_hash(p)
        if p.name in posted or h in posted or h in seen_hashes:
            seen_hashes.add(h)
            continue
        seen_hashes.add(h)
        remaining.append(p)
    if not remaining:
        print("[image] every stashed image has already been posted. add more to images/ to continue.")
        return None
    return random.choice(remaining)


# ---------------------------------------------------------------- state

STATE_FILE = HERE / "state.json"


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(topic: str) -> None:
    state = _load_state()
    recent = state.get("recent_topics", [])
    recent = ([topic.lower()] + recent)[:10]
    state["recent_topics"] = recent
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _save_posted_image(path: Path) -> None:
    state = _load_state()
    posted = state.get("posted_images", [])
    for tag in (path.name, _file_hash(path)):
        if tag not in posted:
            posted.append(tag)
    state["posted_images"] = posted
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="generate but do not post")
    ap.add_argument("--trend", help="override trend selection")
    ap.add_argument("--emit-json", metavar="PATH", help="write live trends JSON for the website and exit")
    ap.add_argument("--hunger-cry", action="store_true", help="force a feral outburst, skipping trend selection")
    ap.add_argument("--post-image", action="store_true", help="post the next unused image from images/ instead of a text tweet")
    ap.add_argument("--text", help="post this exact text, verbatim -- no generation, no trend")
    ap.add_argument("--image", metavar="PATH", help="attach a specific image file (pairs with --text)")
    ap.add_argument("--whoami", action="store_true", help="check which account the current keys post as, without posting")
    args = ap.parse_args()

    if args.whoami:
        whoami()
        return

    if args.emit_json:
        emit_json(args.emit_json)
        return

    # manual override: say exactly this, optionally with an image bolted on
    if args.text or args.image:
        text = args.text or ""
        img = Path(args.image).expanduser() if args.image else None
        if img and not img.exists():
            print(f"[manual] no such file: {img}")
            return
        if not text and not img:
            print("[manual] nothing to say.")
            return
        print("---")
        print(text or "[image only]")
        if img:
            print(f"[attach] {img.name}")
        print(f"--- ({len(text)} chars)")
        if args.dry_run:
            print("[dry-run] not posting.")
            return
        if img:
            tweet_id = post_image_to_x(img, text)
            _save_posted_image(img)
            _save_tweet(text or "[image]", tweet_id, has_image=True)
        else:
            tweet_id = post_to_x(text)
            _save_tweet(text, tweet_id)
        return

    if args.post_image:
        image = pick_unposted_image()
        if not image:
            print("[image] nothing to post.")
            return
        caption = random.choice(IMAGE_CAPTIONS)
        print("---")
        print(f"[image] {image.name}" + (f'  ("{caption}")' if caption else ""))
        print("---")
        if args.dry_run:
            print("[dry-run] not posting.")
            return
        tweet_id = post_image_to_x(image, caption)
        _save_posted_image(image)
        _save_tweet(caption or "[image]", tweet_id, has_image=True)
        return

    trend = None
    cry = _pick_hunger_cry() if args.hunger_cry else (None if args.trend else maybe_hunger_cry())
    if cry:
        tweet = cry
        print("[hunger] the organism skips commentary and just screams")
    else:
        trend = args.trend or pick_trend()
        tweet = generate_with_claude(trend) or generate_from_corpus(trend)

    print("---")
    print(tweet)
    print(f"--- ({len(tweet)} chars)")

    if args.dry_run:
        print("[dry-run] not posting.")
        return

    tweet_id = post_to_x(tweet)
    if trend:
        _save_state(trend)
    _save_tweet(tweet, tweet_id)


if __name__ == "__main__":
    main()
