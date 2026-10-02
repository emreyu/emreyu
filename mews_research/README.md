# Mews Help Center research pipeline

Three stages, run in order from this folder:

| Stage | Script | Output |
|---|---|---|
| 1. Discover | `discover_mews_articles.py` | `mews_articles.json` (every article URL + its Mews topic tags) |
| 2. Fetch | `fetch_mews_articles.py` | `data/articles/<slug>.md` (article text as Markdown, with front matter) |
| 3. Filter | `filter_mews_articles.py` | `output/<topic>/` (matching articles + ranked `index.md`) |

## Setup

```bash
cd mews_research
python3 -m venv .venv                # Windows: py -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

Once the environment is active, `python` and `pip` point to it, so the commands below work as written.
Every new terminal window needs `cd mews_research && source .venv/bin/activate` again before running the scripts.

**Python not found?**
- Mac: macOS has no `python` command, only `python3`. Check with `python3 --version`. If that fails, install Python from <https://www.python.org/downloads/> (or `brew install python` if you use Homebrew).
- Windows: install Python from <https://www.python.org/downloads/> and tick "Add python.exe to PATH" in the installer.

## Run

```bash
# 1. Find every article (takes a while: it opens every topic page)
python discover_mews_articles.py
#    quick check first:  python discover_mews_articles.py --skip-browser

# 2. Download article text (resumable: re-run to continue after a stop)
python fetch_mews_articles.py --limit 5   # test on 5 first, open data/articles/*.md
python fetch_mews_articles.py             # then everything
#    failures are listed in data/fetch_errors.json; add --headed to watch the browser

# 3. Pick out the articles for your topics
python filter_mews_articles.py --dry-run          # see the ranking
python filter_mews_articles.py                    # write output/<topic>/
python filter_mews_articles.py --topic payments   # one topic only
python filter_mews_articles.py --keywords "kiosk,online check-in" --name kiosk   # quick ad-hoc search
```

## Defining your topics

Edit `topics.json`. Each topic has:

- `keywords`: words or phrases to look for. They match whole words and ignore case, and `check-in` also matches `check in`.
- `tags`: text to look for in the Mews topic tags from Stage 1 (part of a tag is enough, so `payment` matches `mews-payments`).
- `exclude`: drop an article if one of these appears in its title or slug.
- `min_score`: the lowest score an article needs to be kept.

How scores add up: +5 per keyword in the title, +3 per keyword in the slug, +4 per matching tag, and +1 per keyword hit in the body (body hits add at most 10).
If a topic pulls in too much, raise `min_score` or add `exclude` words. If it misses articles, add keywords.
Stage 3 runs offline and takes seconds, so adjust and re-run as often as you like.
