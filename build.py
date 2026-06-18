#!/usr/bin/env python3
"""
Note-O-Meter — turn PolitiFact fact-checks into ready-to-file Community Notes.

A PolitiFact-flavored recreation of nonono.leadstories.com. It reads PolitiFact's
RSS feed, scrapes each fact-check for its Truth-O-Meter ruling and debunk summary,
keeps the false ones, asks Claude to draft a neutral Community Note for each, and
renders a static page (docs/index.html) where a noter can grab the note and jump
to the platforms where the claim is spreading.

Public data only — no PolitiFact credentials. The single paid dependency is the
Anthropic API for note drafting (set ANTHROPIC_API_KEY). Drafts are cached by
article URL in data/notes_cache.json so reruns only pay for new fact-checks.

Usage:
    python build.py                 # full build
    python build.py --limit 5       # only process the 5 newest fact-checks
    python build.py --no-llm        # skip Claude; use a template note (free)
    python build.py --rulings false,pants-fire,mostly-false,half-true
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

SITE_TITLE = "Note-O-Meter"
SITE_TAGLINE = "PolitiFact fact-checks, turned into ready-to-file Community Notes."

FEED_URL = "https://www.politifact.com/rss/all/"

# Rulings worth a Community Note — the ones spreading misinformation.
DEFAULT_RULINGS = {"false", "pants-fire", "mostly-false", "barely-true"}

MODEL = "claude-opus-4-8"

# Platforms a noter can search to find posts spreading a claim. {q} is filled
# with a URL-encoded search phrase.
SEARCH_PLATFORMS = [
    ("X", "https://x.com/search?q={q}&f=live"),
    ("Facebook", "https://www.facebook.com/search/posts/?q={q}"),
    ("TikTok", "https://www.tiktok.com/search?q={q}"),
    ("Bluesky", "https://bsky.app/search?q={q}"),
]

# Map PolitiFact ruling slugs to a display label + a CSS class for the badge.
RULING_DISPLAY = {
    "true": ("True", "r-true"),
    "mostly-true": ("Mostly True", "r-mostly-true"),
    "half-true": ("Half True", "r-half-true"),
    "mostly-false": ("Mostly False", "r-mostly-false"),
    "barely-true": ("Mostly False", "r-mostly-false"),  # legacy slug
    "false": ("False", "r-false"),
    "pants-fire": ("Pants on Fire", "r-pof"),
    "full-flop": ("Full Flop", "r-flip"),
    "half-flip": ("Half Flip", "r-flip"),
    "no-flip": ("No Flip", "r-flip"),
}

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "data" / "notes_cache.json"
OUT_PATH = ROOT / "docs" / "index.html"
ASSETS_DIR = ROOT / "docs" / "assets"

# Real PolitiFact Truth-O-Meter graphics, downloaded into docs/assets/ so the
# page is self-contained (no CDN hotlinking). Pants on Fire uses the flame PNG;
# the rest use the dial JPGs.
METER_REMOTE = {
    "true": "https://static.politifact.com/politifact/rulings/meter-true.jpg",
    "mostly-true": "https://static.politifact.com/politifact/rulings/meter-mostly-true.jpg",
    "half-true": "https://static.politifact.com/politifact/rulings/meter-half-true.jpg",
    "mostly-false": "https://static.politifact.com/politifact/rulings/meter-mostly-false.jpg",
    "barely-true": "https://static.politifact.com/politifact/rulings/meter-mostly-false.jpg",
    "false": "https://static.politifact.com/politifact/rulings/meter-false.jpg",
    "pants-fire": "https://static.politifact.com/politifact/rulings/tom_ruling_pof.png",
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# --------------------------------------------------------------------------- #
# Claude note drafting
# --------------------------------------------------------------------------- #

NOTE_SYSTEM_PROMPT = """\
You write proposed Community Notes for human contributors on X, Meta, and \
TikTok. You are given a PolitiFact fact-check of a false or misleading claim. \
Draft a single note a contributor could file on a post repeating that claim.

Rules for the note:
- Be neutral and factual. State what is true; do not editorialize, scold, or \
guess at intent.
- Lead with the correction, plainly. A reader who believed the claim should \
come away informed, not lectured.
- Use only facts supported by the supplied fact-check. Do not add claims, \
statistics, or dates that are not in the material. Never invent dates.
- End with the source: "Source: <the PolitiFact URL>".
- Keep it tight. Community Notes cap at 280 characters and count each URL as \
~1 character, so keep the note body (everything before the URL) under 250 \
characters.
- No hashtags, no emoji, no "PolitiFact rated this" framing — write the \
correction directly, then cite the source.

Also produce a short search phrase (3-6 words, no quotes) capturing the claim, \
so a contributor can find posts spreading it on social platforms.
"""

NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "note": {"type": "string"},
        "search_query": {"type": "string"},
    },
    "required": ["note", "search_query"],
    "additionalProperties": False,
}


def cn_length(text: str) -> int:
    """Community Notes length: URLs count as ~1 character."""
    return len(re.sub(r"https?://\S+", "U", text))


def template_note(fc: dict) -> dict:
    """Free fallback note when --no-llm is set or Claude is unavailable."""
    claim = fc["claim"].strip().strip('"')
    debunk = fc.get("debunk") or "This claim is not accurate."
    note = f"This is misleading. {debunk} Source: {fc['url']}"
    # Trim body if needed (keep the URL).
    if cn_length(note) > 280:
        note = f"Misleading. {debunk[:180].rsplit(' ', 1)[0]}… Source: {fc['url']}"
    query = " ".join(claim.split()[:6])
    return {"note": note, "search_query": query}


def draft_note(client, fc: dict) -> dict:
    """Ask Claude for a proposed note + search phrase. Returns a dict."""
    user = (
        f"CLAIM (verbatim): {fc['claim']}\n"
        f"WHO SAID IT: {fc['claimant']}\n"
        f"POLITIFACT RULING: {fc['ruling_label']}\n"
        f"FACT-CHECK PUBLISHED: {fc['date_display']}\n"
        f"ONE-LINE DEBUNK: {fc.get('debunk', '')}\n"
        f"KEY POINTS:\n{fc.get('summary', '')}\n"
        f"SOURCE URL: {fc['url']}\n"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=NOTE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        output_config={
            "format": {"type": "json_schema", "schema": NOTE_SCHEMA},
            "effort": "low",
        },
    )
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def parse_feed() -> list[dict]:
    """Return fact-check stubs from the RSS feed (no article scraping yet)."""
    feed = feedparser.parse(FEED_URL)
    items = []
    for e in feed.entries:
        link = e.get("link", "")
        if "/factchecks/" not in link:
            continue  # skip analysis articles, lists, etc.
        title = html.unescape(e.get("title", ""))
        claimant = title.split(" - ", 1)[0].strip() if " - " in title else "—"
        claim = html.unescape(e.get("description", "")).strip()
        pubdate = ""
        if getattr(e, "published_parsed", None):
            pubdate = datetime(*e.published_parsed[:6], tzinfo=timezone.utc).isoformat()
        items.append(
            {
                "url": link.replace("http://", "https://"),
                "claimant": claimant,
                "claim": claim,
                "published": pubdate,
            }
        )
    return items


def scrape_article(session: requests.Session, item: dict) -> dict | None:
    """Fetch one fact-check page; attach ruling slug, debunk, and summary."""
    try:
        r = session.get(item["url"], timeout=30)
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"  ! fetch failed {item['url']}: {exc}", file=sys.stderr)
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    # Ruling slug from the share image: rulings/meter-<slug>.jpg
    ruling = None
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        m = re.search(r"meter-([a-z-]+)\.jpg", og["content"])
        if m:
            ruling = m.group(1)
    if not ruling:  # fallback: first statement meter image alt
        img = soup.select_one(".m-statement__meter img[alt]")
        if img:
            ruling = img["alt"].strip().lower().replace(" ", "-")
    item["ruling"] = ruling

    # One-line debunk from the meta description.
    desc = soup.find("meta", attrs={"name": "description"})
    item["debunk"] = html.unescape(desc["content"].strip()) if desc and desc.get("content") else ""

    # "If Your Time Is Short" bullets → key points for the note drafter.
    item["summary"] = extract_short_summary(r.text)
    return item


def extract_short_summary(raw_html: str) -> str:
    """Pull the 'If Your Time Is Short' bullets as plain text."""
    low = raw_html.lower()
    i = low.find("if your time is short")
    if i < 0:
        return ""
    seg = raw_html[i : i + 2500]
    # Cut at common section boundaries that follow the summary.
    for marker in ("see the sources", "our ruling", "our sources"):
        j = seg.lower().find(marker)
        if j > 0:
            seg = seg[:j]
            break
    text = BeautifulSoup(seg, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"^if your time is short\b[:\s]*", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()[:900]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def search_links(query: str) -> list[tuple[str, str]]:
    q = urllib.parse.quote(query)
    return [(name, tmpl.format(q=q)) for name, tmpl in SEARCH_PLATFORMS]


def ensure_meter(slug: str, session: requests.Session) -> str | None:
    """Download the Truth-O-Meter graphic for a ruling into docs/assets/.
    Returns the page-relative path, or None if unavailable."""
    url = METER_REMOTE.get(slug)
    if not url:
        return None
    ext = ".png" if url.endswith(".png") else ".jpg"
    fname = f"meter-{slug}{ext}"
    dest = ASSETS_DIR / fname
    if not dest.exists():
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            ASSETS_DIR.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
        except requests.RequestException as exc:
            print(f"  ! meter fetch failed {slug}: {exc}", file=sys.stderr)
            return None
    return f"assets/{fname}"


def render_card(fc: dict) -> str:
    label = fc["ruling_label"]
    claimant = html.escape(fc["claimant"])
    claim = html.escape(fc["claim"])
    debunk = html.escape(fc.get("debunk", ""))
    note = fc["note"]
    note_attr = html.escape(note, quote=True)
    note_disp = html.escape(note)
    date_disp = html.escape(fc.get("date_display", ""))
    url = html.escape(fc["url"])
    chars = cn_length(note)

    meter = fc.get("meter_src")
    if meter:
        meter_html = (
            f'<img class="meter" src="{html.escape(meter)}" '
            f'alt="PolitiFact Truth-O-Meter rating: {html.escape(label)}" loading="lazy">'
        )
    else:
        meter_html = f'<span class="meter-badge">{html.escape(label)}</span>'

    links = "".join(
        f'<a class="search-link" href="{html.escape(u)}" target="_blank" rel="noopener">{html.escape(n)}</a>'
        for n, u in search_links(fc["search_query"])
    )

    return f"""
    <article class="card">
      <div class="card-grid">
        <div class="meter-col">
          {meter_html}
          <div class="meter-label">{html.escape(label)}</div>
        </div>
        <div class="content-col">
          <div class="meta">{claimant} &middot; {date_disp}</div>
          <p class="claim">&ldquo;{claim}&rdquo;</p>
          <p class="debunk"><span class="nope">No, that&rsquo;s not true:</span> {debunk}</p>

          <div class="note-block">
            <div class="note-label">Proposed Community Note <span class="cc">{chars}/280</span></div>
            <pre class="note" id="note-{fc['id']}">{note_disp}</pre>
            <button class="copy-btn" data-note="{note_attr}">Copy note</button>
          </div>

          <div class="links-row">
            <span class="links-label">Find posts spreading this:</span>
            {links}
          </div>
          <a class="pf-link" href="{url}" target="_blank" rel="noopener">Read the full PolitiFact fact-check &rarr;</a>
        </div>
      </div>
    </article>
    """


def render_page(cards: list[dict]) -> str:
    updated = datetime.now(timezone.utc).strftime("%b %-d, %Y at %-I:%M %p UTC")
    cards_html = "\n".join(render_card(c) for c in cards)
    return PAGE_TEMPLATE.format(
        title=html.escape(SITE_TITLE),
        tagline=html.escape(SITE_TAGLINE),
        updated=html.escape(updated),
        count=len(cards),
        cards=cards_html,
    )


PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} &mdash; PolitiFact Community Notes desk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Public+Sans:ital,wght@0,400;0,600;0,700;0,800;1,400&display=swap" rel="stylesheet">
<style>
  :root {{
    --ink:#1a1b1f; --muted:#6b6e76; --line:#e7ebf2; --bg:#f3f5f8; --card:#fff;
    --blue:#2270fd; --red:#ff0040;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:var(--bg); color:var(--ink);
    font:16px/1.55 'PublicSans','Public Sans',Arial,Helvetica,sans-serif;
    -webkit-font-smoothing:antialiased;
  }}
  a {{ color:var(--blue); }}

  /* PolitiFact masthead */
  .pf-bar {{ background:var(--ink); border-bottom:3px solid var(--red); }}
  .pf-bar .wrap {{
    max-width:860px; margin:0 auto; padding:13px 20px;
    display:flex; align-items:center; justify-content:space-between;
  }}
  .pf-logo {{ color:#fff; font-weight:800; font-size:22px; letter-spacing:-.5px; }}
  .pf-logo b {{ color:var(--red); }}
  .pf-bar-tag {{ color:#b9bcc4; font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:.6px; }}

  .title-band {{ background:var(--card); border-bottom:1px solid var(--line); }}
  .title-band .wrap {{ max-width:860px; margin:0 auto; padding:26px 20px 22px; }}
  .title-band h1 {{ margin:0; font-size:32px; font-weight:800; letter-spacing:-.6px; }}
  .title-band .tagline {{ margin:6px 0 0; color:#3c3f46; font-size:16px; }}
  .title-band .stamp {{ margin:12px 0 0; color:var(--muted); font-size:13px; font-weight:600; }}

  main {{ max-width:860px; margin:0 auto; padding:22px 20px; }}

  .card {{
    background:var(--card); border:1px solid var(--line); border-radius:10px;
    padding:20px 22px; margin:16px 0; box-shadow:0 1px 3px rgba(20,22,30,.05);
  }}
  .card-grid {{ display:flex; gap:22px; align-items:flex-start; }}
  .meter-col {{ flex:0 0 132px; text-align:center; }}
  .meter {{ width:132px; height:auto; display:block; }}
  .meter-badge {{
    display:inline-block; background:var(--red); color:#fff; font-weight:700;
    text-transform:uppercase; font-size:12px; padding:6px 10px; border-radius:5px;
  }}
  .meter-label {{
    margin-top:6px; font-size:12px; font-weight:700; text-transform:uppercase;
    letter-spacing:.5px; color:var(--ink);
  }}
  .content-col {{ flex:1; min-width:0; }}
  .meta {{ color:var(--muted); font-size:13px; font-weight:600; }}
  .claim {{ font-size:20px; line-height:1.35; font-weight:700; margin:6px 0 10px; }}
  .debunk {{ margin:0 0 16px; color:#3c3f46; }}
  .nope {{ color:var(--red); font-weight:800; }}

  .note-block {{ background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:13px 15px; }}
  .note-label {{
    font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.5px;
    color:var(--muted); margin-bottom:9px;
  }}
  .cc {{ float:right; font-weight:600; }}
  pre.note {{
    margin:0 0 11px; white-space:pre-wrap; word-wrap:break-word;
    font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; color:#22242a;
  }}
  .copy-btn {{
    border:0; background:var(--ink); color:#fff; font-size:13px; font-weight:700;
    padding:8px 16px; border-radius:6px; cursor:pointer;
  }}
  .copy-btn:hover {{ background:#000; }}
  .copy-btn.copied {{ background:#1f9d3a; }}

  .links-row {{ margin:16px 0 6px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
  .links-label {{ font-size:13px; color:var(--muted); font-weight:600; }}
  .search-link {{
    font-size:13px; font-weight:600; text-decoration:none; color:var(--ink);
    border:1px solid var(--line); padding:5px 12px; border-radius:999px; background:#fff;
  }}
  .search-link:hover {{ border-color:var(--blue); color:var(--blue); }}
  .pf-link {{ display:inline-block; margin-top:10px; font-size:13px; font-weight:700; color:var(--blue); text-decoration:none; }}
  .pf-link:hover {{ text-decoration:underline; }}

  footer {{ max-width:860px; margin:0 auto; padding:24px 20px 52px; color:var(--muted); font-size:13px; }}
  footer a {{ color:var(--muted); }}

  @media (max-width:620px) {{
    .card-grid {{ flex-direction:column; gap:14px; }}
    .meter-col {{ flex:none; display:flex; align-items:center; gap:12px; text-align:left; }}
    .meter {{ width:96px; }}
    .meter-label {{ margin-top:0; }}
    .title-band h1 {{ font-size:26px; }}
    .claim {{ font-size:18px; }}
  }}
</style>
</head>
<body>
<header class="pf-bar">
  <div class="wrap">
    <span class="pf-logo">Politi<b>Fact</b></span>
    <span class="pf-bar-tag">Community Notes desk</span>
  </div>
</header>
<div class="title-band">
  <div class="wrap">
    <h1>{title}</h1>
    <p class="tagline">{tagline}</p>
    <p class="stamp">{count} fact-checks &middot; updated {updated}</p>
  </div>
</div>
<main>
{cards}
</main>
<footer>
  Drafted notes are starting points, not finished notes &mdash; read the full
  fact-check, confirm the post actually makes the claim, and edit before filing.
  Truth-O-Meter graphics and every fact-check are from
  <a href="https://www.politifact.com">PolitiFact</a>, a project of the Poynter Institute.
</footer>
<script>
  document.querySelectorAll('.copy-btn').forEach(function (btn) {{
    btn.addEventListener('click', function () {{
      navigator.clipboard.writeText(btn.dataset.note).then(function () {{
        var old = btn.textContent;
        btn.textContent = 'Copied!';
        btn.classList.add('copied');
        setTimeout(function () {{ btn.textContent = old; btn.classList.remove('copied'); }}, 1500);
      }});
    }});
  }});
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def load_env() -> None:
    """Load KEY=VALUE pairs from a project-root .env into os.environ (no override)."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def load_cache() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text())
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False))


def date_display(iso: str) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%b %-d, %Y")
    except ValueError:
        return ""


def days_old(iso: str) -> float:
    """Age in days of an ISO date, or a huge number if unparseable."""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except (ValueError, TypeError):
        return 1e9


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None, help="Only process the N newest feed items.")
    ap.add_argument("--no-llm", action="store_true", help="Skip Claude; use template notes.")
    ap.add_argument("--rulings", default=None, help="Comma-separated ruling slugs to keep.")
    ap.add_argument("--window-days", type=int, default=90, help="Show debunks published within N days.")
    args = ap.parse_args()

    load_env()
    rulings = (
        {s.strip() for s in args.rulings.split(",")} if args.rulings else set(DEFAULT_RULINGS)
    )

    # The cache is a rolling archive: each run only sees the latest ~20 feed
    # items, so we accumulate every debunk we've ever scraped and render from
    # the archive (within --window-days), scraping/drafting only new URLs.
    cache = load_cache()

    print("Reading PolitiFact RSS…")
    stubs = parse_feed()
    if args.limit:
        stubs = stubs[: args.limit]
    new_stubs = [s for s in stubs if s["url"] not in cache]
    print(f"  {len(stubs)} fact-checks in feed, {len(new_stubs)} new")

    if new_stubs:
        print("Scraping new rulings…")
        session = make_session()
        scraped = []
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(scrape_article, session, s): s for s in new_stubs}
            for fut in as_completed(futs):
                res = fut.result()
                if res:
                    scraped.append(res)

        client = None
        if not args.no_llm:
            try:
                import anthropic

                client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
            except Exception as exc:  # noqa: BLE001
                print(f"  ! Claude unavailable ({exc}); using template notes", file=sys.stderr)

        new_debunks = [s for s in scraped if s.get("ruling") in rulings]
        print(f"Drafting {len(new_debunks)} new debunk notes…")
        for i, fc in enumerate(new_debunks):
            fc["ruling_label"] = RULING_DISPLAY.get(fc["ruling"], (fc["ruling"], ""))[0]
            fc["date_display"] = date_display(fc.get("published", ""))
            if client:
                try:
                    drafted = draft_note(client, fc)
                except Exception as exc:  # noqa: BLE001
                    print(f"  ! draft failed {fc['url']}: {exc}", file=sys.stderr)
                    drafted = template_note(fc)
            else:
                drafted = template_note(fc)
            print(f"  [{i + 1}/{len(new_debunks)}] {fc['claimant']}")
            cache[fc["url"]] = {
                "url": fc["url"],
                "claimant": fc["claimant"],
                "claim": fc["claim"],
                "debunk": fc.get("debunk", ""),
                "ruling": fc["ruling"],
                "published": fc.get("published", ""),
                "note": drafted["note"],
                "search_query": drafted["search_query"],
                "drafted_at": datetime.now(timezone.utc).isoformat(),
            }
        save_cache(cache)

    # Build the render list from the whole archive, within the recency window.
    cards = [
        rec
        for rec in cache.values()
        if rec.get("ruling") in rulings
        and rec.get("note")
        and days_old(rec.get("published", "")) <= args.window_days
    ]
    meter_session = make_session()
    for fc in cards:
        fc["id"] = re.sub(r"[^a-z0-9]+", "-", fc["url"].rsplit("/factchecks/", 1)[-1]).strip("-")[:60]
        fc["ruling_label"] = RULING_DISPLAY.get(fc["ruling"], (fc["ruling"], ""))[0]
        fc["date_display"] = date_display(fc.get("published", ""))
        fc["meter_src"] = ensure_meter(fc["ruling"], meter_session)
    cards.sort(key=lambda r: r.get("published", ""), reverse=True)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(render_page(cards))
    print(f"Wrote {OUT_PATH} ({len(cards)} cards within {args.window_days} days)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
