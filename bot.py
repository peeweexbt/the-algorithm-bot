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

# How many site-feed slots each dedicated source gets (rest filled from X trends).
MIX = {"crypto": 4, "news": 3}


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


def infer_secondary_platforms(topics: list[str]) -> list[dict]:
    """TikTok/IG have no free trend APIs. The organism extrapolates instead:
    one Claude call infers plausible current trends there from the real X
    trends + headlines + coins it already ingested. Clearly labeled INFERRED."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return []
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 900,
        "system": (
            "You infer what is plausibly trending on TikTok and Instagram right now, "
            "given real currently-trending X topics, news headlines, and coins. "
            "Cross-platform culture overlaps: the same discourse, songs, aesthetics, and "
            "formats travel between apps. Extrapolate formats (sounds, challenges, reel "
            "styles, aesthetics, storytimes) tied to the given topics where natural. "
            "Do NOT invent specific real people or specific events not implied by the "
            "input. Each trend gets a short clinical-eerie 'note' in the voice of a "
            "hive-mind organism describing how it is consuming attention (max 8 words). "
            'Reply ONLY with JSON: {"tiktok":[{"name":"...","note":"..."}],'
            '"instagram":[{"name":"...","note":"..."}]} — 5 items each.'
        ),
        "messages": [{"role": "user", "content": json.dumps(topics)}],
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
        inferred = json.loads(text)
        out = []
        for platform, eye in (("TikTok", "EYE 02"), ("Instagram", "EYE 03")):
            items = inferred.get(platform.lower(), [])[:5]
            if not items:
                continue
            out.append({
                "platform": platform,
                "status": f"{eye} · INFERRED",
                "trends": [
                    {"name": str(it.get("name", ""))[:80],
                     "level": round(max(0.35, 0.9 - i * 0.07), 2),
                     "note": str(it.get("note", ""))[:90]}
                    for i, it in enumerate(items) if it.get("name")
                ],
                "foot": "no direct feed exists. the organism extrapolates from cross-platform residue",
            })
        return out
    except Exception as e:
        print(f"[infer] secondary platforms failed: {e!r}", file=sys.stderr)
        return []


def emit_json(path: str) -> None:
    """Write the observatory feed the website reads. No tweeting.

    Slots are filled per MIX from the dedicated crypto + politics feeds, the
    rest from X trends ranked by WEIGHTS. Weight-0 categories never appear.
    """
    pool = build_pool()
    by_cat: dict[str, list[str]] = {}
    for t, c in pool:
        by_cat.setdefault(c, []).append(t)
    picked: list[tuple[str, str]] = []
    for cat, n in MIX.items():
        picked += [(t, cat) for t in by_cat.get(cat, [])[:n]]
    rest = [(t, c) for t, c in pool if (t, c) not in picked]
    rest.sort(key=lambda tc: -WEIGHTS.get(tc[1], 2))
    picked += rest[: max(0, 9 - len(picked))]
    ranked = picked[:9]
    tags = dict(ranked)
    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platforms": [{
            "platform": "X",
            "status": "EYE 01 · OPEN · LIVE",
            "trends": [
                {
                    "name": t,
                    "level": round(max(0.35, 0.96 - i * 0.045), 2),
                    "note": c.replace("_", " ") + " · "
                            + random.choice(NOTES).format(p=int(max(35, 96 - i * 4.5))),
                }
                for i, (t, c) in enumerate(ranked)
            ],
            "foot": "live ingestion via public trend telemetry",
        }] + infer_secondary_platforms([t for t, _ in ranked]),
    }
    Path(path).write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"[emit] wrote {path} ({len(out['platforms'])} platforms, {len(ranked)} X trends)")


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


TWEETS_FILE = HERE / "tweets.json"


def _save_tweet(text: str, tweet_id: str) -> None:
    """Archive posted tweets for the website's live transmissions section."""
    try:
        items = json.loads(TWEETS_FILE.read_text())
    except Exception:
        items = []
    items.insert(0, {
        "text": text,
        "id": tweet_id,
        "url": f"https://x.com/i/status/{tweet_id}",
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    TWEETS_FILE.write_text(json.dumps(items[:12], indent=2, ensure_ascii=False))


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
    STATE_FILE.write_text(json.dumps({"recent_topics": recent}, indent=2))


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="generate but do not post")
    ap.add_argument("--trend", help="override trend selection")
    ap.add_argument("--emit-json", metavar="PATH", help="write live trends JSON for the website and exit")
    args = ap.parse_args()

    if args.emit_json:
        emit_json(args.emit_json)
        return

    trend = args.trend or pick_trend()
    tweet = generate_with_claude(trend) or generate_from_corpus(trend)

    print("---")
    print(tweet)
    print(f"--- ({len(tweet)} chars)")

    if args.dry_run:
        print("[dry-run] not posting.")
        return

    tweet_id = post_to_x(tweet)
    _save_state(trend)
    _save_tweet(tweet, tweet_id)


if __name__ == "__main__":
    main()
