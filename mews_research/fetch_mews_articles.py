"""
Mews Help Center - article fetcher (Stage 2 of the research agent).

Reads mews_articles.json from Stage 1, renders each article in a browser and
saves it as Markdown with front matter to data/articles/<slug>.md.
Already-fetched articles are skipped, so the run can be stopped and resumed.

Setup:
  pip install -r requirements.txt
  playwright install chromium

Run:
  python fetch_mews_articles.py                    # everything not fetched yet
  python fetch_mews_articles.py --limit 5          # quick test
  python fetch_mews_articles.py --only-slugs a,b   # specific articles
  python fetch_mews_articles.py --refresh          # re-fetch everything
"""
import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from markdownify import markdownify
from playwright.async_api import async_playwright

from discover_mews_articles import DELAY_S, UA, dismiss_cookies, goto

# Candidate containers for the article body, most specific first.
# Salesforce Knowledge renders rich text with Aura (uiOutputRichText) or LWC.
BODY_SELECTORS = [
    ".uiOutputRichText",
    "lightning-formatted-rich-text",
    ".slds-rich-text-editor__output",
    "[class*='article-body']",
    "[class*='articleBody']",
    "article",
    "main",
]
MIN_BODY_CHARS = 200
LAST_MOD_RE = re.compile(r"last\s+modified(?:\s+date)?\s*:?\s*([^\n]+)", re.I)

# Returns the element's light DOM plus its shadow root, so LWC content is kept.
OUTER_HTML_JS = """e => {
  const parts = [];
  if (e.shadowRoot) parts.push(e.shadowRoot.innerHTML);
  parts.push(e.innerHTML);
  return {html: parts.join('\\n'), text: (e.innerText || e.textContent || '').trim()};
}"""


# Fallback when no selector matches: score every element by the text of the
# paragraphs/list items below it (piercing open shadow roots) and keep the best
# one outside header/nav/footer - a tiny "readability" heuristic.
DENSEST_BLOCK_JS = """() => {
  const SKIP = /^(NAV|HEADER|FOOTER|ASIDE|SCRIPT|STYLE|NOSCRIPT|FORM|BUTTON)$/;
  const blocks = [];
  const walk = root => {
    root.querySelectorAll('p, li, td, pre, h2, h3, h4').forEach(e => blocks.push(e));
    root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) walk(e.shadowRoot); });
  };
  walk(document);
  const up = e => e.parentElement || (e.parentNode && e.parentNode.host) || null;
  const scores = new Map();
  for (const b of blocks) {
    const len = (b.textContent || '').trim().length;
    if (len < 25) continue;
    let w = 1;
    for (let a = up(b), d = 0; a && d < 3; a = up(a), d++, w /= 2) {
      scores.set(a, (scores.get(a) || 0) + len * w);
    }
  }
  let best = null, bestScore = 0;
  for (const [el, sc] of scores) {
    if (sc <= bestScore) continue;
    let skip = false;
    for (let a = el; a; a = up(a)) if (SKIP.test(a.tagName || '')) { skip = true; break; }
    if (!skip) { best = el; bestScore = sc; }
  }
  if (!best) return {html: '', text: ''};
  const html = (best.shadowRoot ? best.shadowRoot.innerHTML + '\\n' : '') + best.innerHTML;
  return {html, text: (best.innerText || best.textContent || '').trim()};
}"""


def front_matter(meta):
    # JSON scalars and lists are valid YAML, and trivial to parse back in Stage 3.
    lines = [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items()]
    return "---\n" + "\n".join(lines) + "\n---\n\n"


def to_markdown(html):
    md = markdownify(html, heading_style="ATX", strip=["script", "style"])
    return re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"


async def extract(page):
    """Return (title, body_html, last_modified) for the rendered article."""
    body_html, best_len = "", 0
    for sel in BODY_SELECTORS:
        loc = page.locator(sel)
        for i in range(await loc.count()):
            try:
                r = await loc.nth(i).evaluate(OUTER_HTML_JS)
            except Exception:
                continue
            if len(r["text"]) > best_len:
                body_html, best_len = r["html"], len(r["text"])
        if best_len >= MIN_BODY_CHARS:
            break  # a specific selector worked; don't fall back to the whole page
    if best_len < MIN_BODY_CHARS:
        r = await page.evaluate(DENSEST_BLOCK_JS)
        if len(r["text"]) > best_len:
            body_html, best_len = r["html"], len(r["text"])

    title = ""
    h1 = page.locator("h1")
    if await h1.count():
        title = (await h1.first.inner_text()).strip()
    if not title:
        title = (await page.title()).split("|")[0].strip()

    last_modified = None
    lm = page.get_by_text(re.compile(r"last\s+modified", re.I))
    if await lm.count():
        try:
            txt = await lm.first.locator("xpath=..").inner_text()
            m = LAST_MOD_RE.search(txt)
            if m:
                last_modified = m.group(1).strip()
        except Exception:
            pass

    return title, body_html, last_modified


async def save_debug(page, debug_dir, slug):
    """Keep the rendered page so a failed extraction can be inspected."""
    debug_dir.mkdir(parents=True, exist_ok=True)
    try:
        (debug_dir / f"{slug}.html").write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(debug_dir / f"{slug}.png"), full_page=True)
    except Exception as e:
        print(f"    (could not save debug files: {e})")


async def run(rows, out_dir, headed):
    errors = []
    debug_dir = out_dir.parent / "debug"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headed)
        ctx = await browser.new_context(user_agent=UA, locale="en-US")
        page = await ctx.new_page()
        cookies_done = False

        for i, row in enumerate(rows, 1):
            slug = row["slug"]
            try:
                await goto(page, row["url"].split("?")[0])
                if not cookies_done:
                    await dismiss_cookies(page)
                    cookies_done = True
                try:  # wait for any known body container, not each one in turn
                    await page.locator(", ".join(BODY_SELECTORS[:5])).first.wait_for(timeout=15_000)
                except Exception:
                    pass
                title, html, last_modified = await extract(page)
                body = to_markdown(html) if html else ""
                if len(body) < 50:
                    await save_debug(page, debug_dir, slug)
                    raise RuntimeError(
                        f"article body not found (page saved to {debug_dir}/{slug}.html/.png)"
                    )
                meta = {
                    "url": row["url"],
                    "slug": slug,
                    "title": title,
                    "topics": row.get("topics", []),
                    "last_modified": last_modified,
                    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                text = front_matter(meta) + f"# {title}\n\n" + body
                (out_dir / f"{slug}.md").write_text(text, encoding="utf-8")
                print(f"  [{i}/{len(rows)}] {slug}: {len(body)} chars")
            except Exception as e:
                print(f"  ! [{i}/{len(rows)}] {slug}: {e}")
                errors.append({"slug": slug, "url": row["url"], "error": str(e)})
            await page.wait_for_timeout(DELAY_S * 1000)

        await browser.close()
    return errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="mews_articles.json")
    ap.add_argument("--out-dir", default="data/articles")
    ap.add_argument("--limit", type=int, default=0, help="fetch at most N articles")
    ap.add_argument("--only-slugs", default="", help="comma-separated slugs")
    ap.add_argument("--refresh", action="store_true", help="re-fetch existing files")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    rows = json.loads(Path(args.inp).read_text(encoding="utf-8"))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.only_slugs:
        wanted = {s.strip() for s in args.only_slugs.split(",") if s.strip()}
        rows = [r for r in rows if r["slug"] in wanted]
    if not args.refresh:
        rows = [r for r in rows if not (out_dir / f"{r['slug']}.md").exists()]
    if args.limit:
        rows = rows[: args.limit]

    print(f"{len(rows)} articles to fetch -> {out_dir}")
    errors = asyncio.run(run(rows, out_dir, args.headed)) if rows else []

    err_path = out_dir.parent / "fetch_errors.json"
    err_path.write_text(json.dumps(errors, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\ndone: {len(rows) - len(errors)} saved, {len(errors)} failed (see {err_path})")


if __name__ == "__main__":
    main()
