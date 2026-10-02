"""
Mews Help Center - article discovery (Stage 1 of the research agent).

Collects every article URL on help.mews.com plus the topics it appears under.
It does NOT fetch article bodies; that is Stage 2.

Strategy:
  1. Sitemap (cheap): robots.txt -> declared sitemaps, then /s/sitemap.xml, /sitemap.xml
  2. Browser (needed for topic tags): render /s/topiccatalog, open every topic page,
     expand "View more" / "Load more", collect /s/article/ links.
     Fallback: click each "Read All Articles" tile on the home page.

Setup:
  pip install playwright requests
  playwright install chromium

Run:
  python discover_mews_articles.py                  # sitemap + browser (recommended)
  python discover_mews_articles.py --skip-browser   # sitemap only, no topic tags
  python discover_mews_articles.py --headed         # watch the browser, useful for debugging
"""
import argparse
import asyncio
import json
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import requests
from playwright.async_api import async_playwright

BASE = "https://help.mews.com"
LANG = "en_US"
DELAY_S = 1.0  # politeness delay between requests
UA = "Mozilla/5.0 (product-research crawler)"
ARTICLE_RE = re.compile(r"/s/article/[^/?#]+")
TOPIC_RE = re.compile(r"/s/topic/[^?#]+")
SM_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def norm_article(href):
    """Return canonical article URL (no query string) or None."""
    if not href:
        return None
    path = urlparse(href if href.startswith("http") else BASE + href).path
    m = ARTICLE_RE.search(path)
    return f"{BASE}{m.group(0)}" if m else None


def add(out, url, source, topic=None):
    rec = out.setdefault(url, {"topics": set(), "sources": set()})
    rec["sources"].add(source)
    if topic:
        rec["topics"].add(topic)


# ---------------------------------------------------------------- sitemap ---

def sitemap_candidates():
    cands = []
    try:
        r = requests.get(f"{BASE}/robots.txt", headers={"User-Agent": UA}, timeout=20)
        for line in r.text.splitlines():
            if line.lower().startswith("sitemap:"):
                cands.append(line.split(":", 1)[1].strip())
    except requests.RequestException:
        pass
    cands += [f"{BASE}/s/sitemap.xml", f"{BASE}/sitemap.xml"]
    return list(dict.fromkeys(cands))


def crawl_sitemap(url, seen, out, depth=0):
    if url in seen or depth > 4:
        return
    seen.add(url)
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        if r.status_code != 200:
            return
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError):
        return
    for loc in root.findall(".//sm:sitemap/sm:loc", SM_NS):  # sitemap index
        time.sleep(DELAY_S)
        crawl_sitemap(loc.text.strip(), seen, out, depth + 1)
    for loc in root.findall(".//sm:url/sm:loc", SM_NS):
        u = norm_article(loc.text.strip())
        if u:
            add(out, u, "sitemap")


# ---------------------------------------------------------------- browser ---

async def all_hrefs(page):
    # Playwright CSS locators pierce open shadow DOM (Salesforce LWC).
    return await page.locator("a[href]").evaluate_all(
        "els => els.map(e => e.getAttribute('href'))"
    )


async def goto(page, url):
    sep = "&" if "?" in url else "?"
    await page.goto(f"{url}{sep}language={LANG}", wait_until="networkidle", timeout=90_000)
    await page.wait_for_timeout(1500)  # Lightning keeps rendering after networkidle


async def dismiss_cookies(page):
    try:  # privacy-preserving choice
        await page.get_by_text("Decline", exact=True).first.click(timeout=4000)
    except Exception:
        pass


async def expand(page, max_clicks=60):
    """Scroll and click 'view/load/show more' until nothing is left."""
    more = re.compile(r"(view|load|show)\s+more", re.I)
    for _ in range(max_clicks):
        await page.mouse.wheel(0, 20_000)
        await page.wait_for_timeout(800)
        btn = page.get_by_role("button", name=more)
        if await btn.count() == 0:
            btn = page.get_by_text(more)
        if await btn.count() == 0 or not await btn.first.is_visible():
            return
        try:
            await btn.first.click(timeout=10_000)
        except Exception:
            # Something (sticky banner, overlay) covers the button: click it via JS.
            try:
                await btn.first.dispatch_event("click")
            except Exception:
                return  # keep whatever has loaded so far
        await page.wait_for_timeout(1500)


async def topics_from_catalog(page):
    await goto(page, f"{BASE}/s/topiccatalog")
    await dismiss_cookies(page)
    await expand(page)
    found = set()
    for h in await all_hrefs(page):
        m = TOPIC_RE.search(h or "")
        if m:
            found.add(f"{BASE}{m.group(0)}")
    return sorted(found)


async def topics_from_home(page):
    """Fallback: home tiles use javascript:void links, so click them one by one."""
    urls = []
    await goto(page, f"{BASE}/s/")
    await dismiss_cookies(page)
    n = await page.get_by_text("Read All Articles").count()
    for i in range(n):
        try:
            await goto(page, f"{BASE}/s/")
            await page.get_by_text("Read All Articles").nth(i).click()
            await page.wait_for_url(TOPIC_RE, timeout=20_000)
            urls.append(page.url.split("?")[0])
        except Exception as e:
            print(f"  ! home tile {i}: {e}")
    return sorted(set(urls))


async def crawl_browser(out, headed=False):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headed)
        ctx = await browser.new_context(user_agent=UA, locale="en-US")
        page = await ctx.new_page()

        topics = await topics_from_catalog(page)
        if not topics:
            print("[browser] topic catalog empty, falling back to home tiles")
            topics = await topics_from_home(page)
        print(f"[browser] {len(topics)} topics")

        for i, t in enumerate(topics, 1):
            name = t.rstrip("/").split("/")[-1]
            try:
                await goto(page, t)
                await expand(page)
                n = 0
                for h in await all_hrefs(page):
                    u = norm_article(h)
                    if u:
                        add(out, u, "browser", name)
                        n += 1
                print(f"  [{i}/{len(topics)}] {name}: {n} article links")
            except Exception as e:
                print(f"  ! {name}: {e}")
            await page.wait_for_timeout(DELAY_S * 1000)

        await browser.close()


# ------------------------------------------------------------------- main ---

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-sitemap", action="store_true")
    ap.add_argument("--skip-browser", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--out", default="mews_articles.json")
    args = ap.parse_args()

    out = {}
    if not args.skip_sitemap:
        seen = set()
        for sm in sitemap_candidates():
            crawl_sitemap(sm, seen, out)
        print(f"[sitemap] {len(out)} articles")
    if not args.skip_browser:
        asyncio.run(crawl_browser(out, headed=args.headed))

    rows = [
        {
            "url": f"{u}?language={LANG}",
            "slug": u.rsplit("/", 1)[-1],
            "topics": sorted(r["topics"]),
            "sources": sorted(r["sources"]),
        }
        for u, r in sorted(out.items())
    ]
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)

    untagged = sum(1 for r in rows if not r["topics"])
    print(f"\n{len(rows)} unique articles -> {args.out} ({untagged} without topic tags)")


if __name__ == "__main__":
    main()
