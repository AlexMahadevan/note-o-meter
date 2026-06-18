# Note-O-Meter

A PolitiFact-flavored recreation of [nonono.leadstories.com](https://nonono.leadstories.com/).
It turns PolitiFact fact-checks into ready-to-file **Community Notes**: each
debunked claim gets a neutral proposed note, search links to find posts
spreading the claim, and a link to the full fact-check.

Live page: `docs/index.html` (designed for GitHub Pages).

## How it works

```
PolitiFact RSS ─► scrape each fact-check ─► keep the false ones ─► Claude drafts
   (claim,           (Truth-O-Meter ruling,      (False, Pants on      a neutral note
    claimant,         one-line debunk,            Fire, Mostly           + search phrase
    date, link)       "If Your Time Is Short")    False)                      │
                                                                              ▼
                                              rolling archive (data/notes_cache.json)
                                                                              │
                                                                              ▼
                                                  static page (docs/index.html)
```

- **Data source:** public PolitiFact RSS + page scraping. No PolitiFact
  credentials. The ruling comes from each page's `og:image` (`meter-<slug>.jpg`);
  the debunk summary from the meta description and the "If Your Time Is Short"
  box.
- **Notes:** drafted by Claude (`claude-opus-4-8`) — neutral, factual, ≤280
  characters (URLs count as ~1), ending with the PolitiFact source link.
- **Rolling archive:** the RSS feed only carries the ~20 newest items, so every
  debunk is cached in `data/notes_cache.json` and the page renders everything
  seen within `--window-days` (default 90). Each run only scrapes/drafts genuinely
  new fact-checks, so reruns are nearly free.

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env   # gitignored
```

## Run

```bash
./venv/bin/python build.py                 # full build → docs/index.html
./venv/bin/python build.py --limit 5       # only the 5 newest feed items
./venv/bin/python build.py --no-llm        # free: template notes, no Claude
./venv/bin/python build.py --window-days 30
./venv/bin/python build.py --rulings false,pants-fire,mostly-false,half-true
```

Open `docs/index.html` in a browser. Click **Copy note** to grab a draft; use
the platform links to find posts repeating the claim.

## Publish (GitHub Pages)

1. Create a GitHub repo and add it as `origin`.
2. In repo Settings → Pages, serve from the `main` branch, `/docs` folder.
3. `bash tools/refresh.sh` builds, commits `docs/` + `data/`, and pushes.

## Schedule (daily)

A launchd agent (macOS) or cron entry that runs `tools/refresh.sh` once a day
keeps the page current and the archive growing. Example cron:

```
0 8 * * *  /bin/bash /Users/alexmahadevan/python_projects/pf-nonono/tools/refresh.sh >> /tmp/pf-nonono.log 2>&1
```

## Config

Edit the constants at the top of `build.py`: `SITE_TITLE`, `SITE_TAGLINE`,
`DEFAULT_RULINGS`, `SEARCH_PLATFORMS`, `MODEL`.

## Notes are starting points

Drafts are not finished notes. Read the full fact-check, confirm the post
actually makes the claim, and edit before filing. Single source by design:
every entry is a PolitiFact fact-check.
