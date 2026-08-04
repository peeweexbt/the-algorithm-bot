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
    """Requires paid (Basic+) X API read access. Silently skipped otherwise."""
    bearer = os.getenv("X_BEARER_TOKEN")
    if not bearer:
        return []
    try:
        req = urllib.request.Request(
            "https://api.x.com/2/trends/by/woeid/1",  # 1 = worldwide
            headers={"Authorization": f"Bearer {bearer}", **UA},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        return [t["trend_name"] for t in data.get("data", [])][:20]
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
    "meme": 10,
    "pop_culture": 8,
    "finance": 8,       # traditional markets: stocks, fed, earnings
    "news": 6,
    "other": 3,
    "sports": 1,
}


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
    trends = get_trends()
    recent = _load_state().get("recent_topics", [])
    fresh = [t for t in trends if t.lower() not in recent] or trends
    tags = classify(fresh)
    weights = [WEIGHTS.get(tags.get(t, "other"), 3) for t in fresh]
    if sum(weights) == 0:
        weights = [1] * len(fresh)
    choice = random.choices(fresh, weights=weights, k=1)[0]
    print(f"[trends] picked={choice!r} category={tags.get(choice)}")
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

    Trends are reordered by taste: preferred categories (see WEIGHTS) float to
    the top; anything with weight 0 is dropped entirely.
    """
    trends = get_trends()
    tags = classify(trends)
    ranked = sorted(
        [t for t in trends if WEIGHTS.get(tags.get(t, "other"), 3) > 0],
        key=lambda t: (-WEIGHTS.get(tags.get(t, "other"), 3), trends.index(t)),
    )[:8]
    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platforms": [{
            "platform": "X",
            "status": "EYE 01 · OPEN · LIVE",
            "trends": [
                {
                    "name": t,
                    "level": round(max(0.35, 0.96 - i * 0.045), 2),
                    "note": tags.get(t, "other").replace("_", " ") + " · "
                            + random.choice(NOTES).format(p=int(max(35, 96 - i * 4.5))),
                }
                for i, t in enumerate(ranked)
            ],
            "foot": "live ingestion via public trend telemetry",
        }],
    }
    Path(path).write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"[emit] wrote {path} ({len(ranked)} trends)")


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

def post_to_x(text: str) -> None:
    import tweepy  # imported here so --dry-run works without it installed

    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )
    resp = client.create_tweet(text=text)
    print(f"[post] tweeted: https://x.com/i/status/{resp.data['id']}")


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

    post_to_x(tweet)
    _save_state(trend)


if __name__ == "__main__":
    main()
