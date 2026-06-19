#!/usr/bin/env python3
"""
viral.py — find high-reach posts that PolitiFact has already debunked.

Pulls posts eligible for Community Notes from X's Community Notes API (with
engagement metrics), ranks them by reach, then for the top posts:
  1. Claude extracts the central checkable claim + a PolitiFact search query.
  2. We search PolitiFact.com live for that claim (false-rated checks only).
  3. Claude confirms which result the post actually repeats.
  4. We attach our already-drafted note (if we have one) or draft a fresh one.

Confirmed matches go to data/viral_matches.json, which build.py renders as the
"Going viral — already debunked" section so a human can go file the note.

Read-only against X (never submits a note). Reuses the note-writer's X OAuth1
keys (X_API_KEY etc., in .env). Best-effort: failures leave prior matches in
place so the site still builds.

    ./venv/bin/python viral.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone

import requests
from requests_oauthlib import OAuth1Session

from build import (
    CACHE_PATH,
    ROOT,
    RULING_DISPLAY,
    date_display,
    draft_note,
    load_cache,
    load_env,
    make_session,
    save_cache,
    scrape_article,
)

MATCHES_PATH = ROOT / "data" / "viral_matches.json"
ELIGIBLE_URL = "https://api.x.com/2/notes/search/posts_eligible_for_notes"
PF_SEARCH = "https://www.politifact.com/search/"
MODEL = "claude-opus-4-8"
TOP_POSTS = 40

# PolitiFact search scraping (ported from the note-writer bot).
_URL_RE = re.compile(r"/factchecks/(\d{4})/([a-z]+)/(\d+)/([a-z0-9-]+)/([a-z0-9-]+)/")
_METER_RE = re.compile(r"meter-(false|true|mostly-false|mostly-true|half-true|pants-on-fire|pants-fire|full-flop|half-flop|no-flip)")
_FALSE = {"false", "mostly-false", "pants-on-fire", "pants-fire", "barely-true"}
_WINDOW = 3000
_RULING_NORM = {"pants-on-fire": "pants-fire", "barely-true": "mostly-false"}

EXTRACT_SYSTEM = """\
For each social post, decide if it makes a specific, checkable factual claim \
(not a joke, opinion, question, ad, or announcement) and, if so, give a short \
3-6 word PolitiFact search query capturing the claim. If it is not a checkable \
claim, set checkable=false and leave query empty."""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "checkable": {"type": "boolean"},
                    "query": {"type": "string"},
                },
                "required": ["i", "checkable", "query"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

CONFIRM_SYSTEM = """\
You decide whether a viral post repeats the same false claim that a PolitiFact \
fact-check debunks — closely enough that the fact-check is the right context to \
add to the post. Be strict: the post must assert or endorse the claim, not just \
mention the topic, ask about it, joke about it, or debunk it. Pick the single \
best-matching candidate, or none."""

CONFIRM_SCHEMA = {
    "type": "object",
    "properties": {
        "candidate": {"type": "integer", "description": "index of the matching fact-check, or -1 for none"},
        "why": {"type": "string"},
    },
    "required": ["candidate", "why"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- #
# X — eligible posts with engagement
# --------------------------------------------------------------------------- #


def x_session() -> OAuth1Session:
    return OAuth1Session(
        client_key=os.environ["X_API_KEY"],
        client_secret=os.environ.get("X_API_KEY_SECRET") or os.environ["X_API_SECRET"],
        resource_owner_key=os.environ["X_ACCESS_TOKEN"],
        resource_owner_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )


def fetch_eligible(test_mode: bool = True, max_results: int = 100) -> list[dict]:
    qs = {
        "test_mode": str(test_mode).lower(),
        "max_results": str(max_results),
        "tweet.fields": "public_metrics,created_at",
        "expansions": "author_id",
        "user.fields": "username,public_metrics",
    }
    r = x_session().get(f"{ELIGIBLE_URL}?{urllib.parse.urlencode(qs)}", timeout=30)
    r.raise_for_status()
    payload = r.json()
    users = {u["id"]: u for u in payload.get("includes", {}).get("users", [])}
    posts = []
    for p in payload.get("data", []):
        pm = p.get("public_metrics", {}) or {}
        u = users.get(p.get("author_id"), {})
        posts.append(
            {
                "id": str(p["id"]),
                "text": p.get("text", ""),
                "username": u.get("username"),
                "created_at": p.get("created_at"),
                "metrics": {
                    "impressions": pm.get("impression_count", 0),
                    "likes": pm.get("like_count", 0),
                    "retweets": pm.get("retweet_count", 0),
                    "replies": pm.get("reply_count", 0),
                    "quotes": pm.get("quote_count", 0),
                    "bookmarks": pm.get("bookmark_count", 0),
                },
            }
        )
    return posts


def reach(post: dict) -> int:
    m = post["metrics"]
    return m["impressions"] or (m["likes"] + 3 * m["retweets"] + 2 * m["replies"] + 5 * m["quotes"])


def post_url(p: dict) -> str:
    return f"https://x.com/{p['username']}/status/{p['id']}" if p.get("username") else f"https://x.com/i/web/status/{p['id']}"


# --------------------------------------------------------------------------- #
# PolitiFact live search
# --------------------------------------------------------------------------- #


def politifact_search(query: str, session: requests.Session, max_results: int = 6) -> list[dict]:
    try:
        r = session.get(PF_SEARCH, params={"q": query}, timeout=15)
        r.raise_for_status()
    except requests.RequestException:
        return []
    html = r.text
    out, seen = [], set()
    for m in _URL_RE.finditer(html):
        path = m.group(0)
        if path in seen:
            continue
        seen.add(path)
        year, mon, day, claimant, slug = m.groups()
        meters = _METER_RE.findall(html[max(0, m.start() - _WINDOW): m.end() + _WINDOW])
        rating = meters[0] if meters else None
        if not rating or rating not in _FALSE:
            continue
        out.append(
            {
                "url": "https://www.politifact.com" + path,
                "ruling": _RULING_NORM.get(rating, rating),
                "claimant": " ".join(w.capitalize() for w in claimant.split("-")),
                "claim": slug.replace("-", " "),
            }
        )
        if len(out) >= max_results:
            break
    return out


# --------------------------------------------------------------------------- #
# Claude steps
# --------------------------------------------------------------------------- #


def _client():
    import anthropic

    return anthropic.Anthropic()


def extract_claims(client, posts: list[dict]) -> dict[int, str]:
    listing = "\n".join(f"[{i}] {p['text'][:300]}" for i, p in enumerate(posts))
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": f"POSTS:\n{listing}\n\nReturn one item per post."}],
        output_config={"format": {"type": "json_schema", "schema": EXTRACT_SCHEMA}, "effort": "low"},
    )
    text = next(b.text for b in resp.content if b.type == "text")
    return {
        it["i"]: it["query"].strip()
        for it in json.loads(text).get("items", [])
        if it.get("checkable") and it.get("query", "").strip()
    }


def confirm(client, post_text: str, candidates: list[dict]) -> tuple[int, str]:
    listing = "\n".join(
        f'[{j}] "{c["claim"]}" — {c["claimant"]} (PolitiFact: {c["ruling"]})' for j, c in enumerate(candidates)
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=600,
        system=CONFIRM_SYSTEM,
        messages=[{"role": "user", "content": f"POST:\n{post_text[:600]}\n\nCANDIDATE FACT-CHECKS:\n{listing}"}],
        output_config={"format": {"type": "json_schema", "schema": CONFIRM_SCHEMA}, "effort": "low"},
    )
    text = next(b.text for b in resp.content if b.type == "text")
    d = json.loads(text)
    return d.get("candidate", -1), d.get("why", "")


# --------------------------------------------------------------------------- #
# Note attach / draft
# --------------------------------------------------------------------------- #


def note_for(url: str, candidate: dict, post_text: str, cache: dict, http: requests.Session, client) -> dict | None:
    """Return a card payload {ruling, ruling_label, claimant, claim, note} for a
    matched PolitiFact URL — from cache if we have it, else scrape + draft."""
    cached = cache.get(url)
    if cached and cached.get("note"):
        label = RULING_DISPLAY.get(cached["ruling"], (cached["ruling"], ""))[0]
        return {
            "ruling": cached["ruling"], "ruling_label": label,
            "claimant": cached["claimant"], "claim": cached["claim"], "note": cached["note"],
        }
    # New debunk: scrape the PolitiFact page, draft a note, cache it.
    item = {"url": url, "claimant": candidate["claimant"], "claim": candidate["claim"], "published": ""}
    scraped = scrape_article(http, item)
    if not scraped or not scraped.get("ruling"):
        return None
    scraped["ruling_label"] = RULING_DISPLAY.get(scraped["ruling"], (scraped["ruling"], ""))[0]
    scraped["date_display"] = ""
    try:
        drafted = draft_note(client, scraped)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! draft failed for {url}: {exc}", file=sys.stderr)
        return None
    cache[url] = {
        "url": url, "claimant": scraped["claimant"], "claim": scraped.get("debunk") or candidate["claim"],
        "debunk": scraped.get("debunk", ""), "ruling": scraped["ruling"], "published": "",
        "note": drafted["note"], "search_query": drafted["search_query"],
        "drafted_at": datetime.now(timezone.utc).isoformat(), "source": "viral",
    }
    return {
        "ruling": scraped["ruling"], "ruling_label": scraped["ruling_label"],
        "claimant": scraped["claimant"], "claim": scraped.get("debunk") or candidate["claim"], "note": drafted["note"],
    }


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main() -> int:
    load_env()
    test_mode = os.getenv("TEST_MODE", "true").strip().lower() != "false"

    try:
        posts = fetch_eligible(test_mode=test_mode)
    except Exception as exc:  # noqa: BLE001
        print(f"! eligible fetch failed: {exc}", file=sys.stderr)
        return 1
    posts.sort(key=reach, reverse=True)
    top = posts[:TOP_POSTS]
    print(f"eligible: {len(posts)} · matching top {len(top)} (test_mode={test_mode})")
    if not top:
        write([], test_mode)
        return 0

    client = _client()
    http = make_session()
    pf = requests.Session()
    pf.headers.update({"User-Agent": "Mozilla/5.0 (compatible; NoteOMeter/1.0; +https://www.poynter.org)"})
    cache = load_cache()

    queries = extract_claims(client, top)
    print(f"checkable posts: {len(queries)}")

    matches = []
    for i, q in queries.items():
        p = top[i]
        cands = politifact_search(q, pf)
        if not cands:
            continue
        ci, why = confirm(client, p["text"], cands)
        if ci < 0 or ci >= len(cands):
            continue
        cand = cands[ci]
        payload = note_for(cand["url"], cand, p["text"], cache, http, client)
        if not payload:
            continue
        matches.append(
            {
                "post_id": p["id"], "url": post_url(p), "username": p.get("username"),
                "text": p["text"], "created_at": p.get("created_at"),
                "metrics": p["metrics"], "reach": reach(p), "why": why,
                "factcheck": {**payload, "url": cand["url"]},
            }
        )
        print(f"  ⚑ {p['metrics']['impressions']:>10,} impressions · {payload['ruling_label']:13s} · {p['text'][:60]!r}")

    matches.sort(key=lambda x: x["reach"], reverse=True)
    save_cache(cache)
    write(matches, test_mode)
    print(f"matches: {len(matches)}")
    return 0


def write(matches: list[dict], test_mode: bool) -> None:
    MATCHES_PATH.parent.mkdir(parents=True, exist_ok=True)
    MATCHES_PATH.write_text(
        json.dumps(
            {"generated_at": datetime.now(timezone.utc).isoformat(), "test_mode": test_mode,
             "count": len(matches), "matches": matches},
            indent=2, ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
