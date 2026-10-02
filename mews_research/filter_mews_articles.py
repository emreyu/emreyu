"""
Mews Help Center - topic filter (Stage 3 of the research agent).

Scores every fetched article (data/articles/*.md) against the topics in
topics.json and copies the matches to output/<topic>/ with a ranked index.md.

Scoring, per topic (keywords match whole words, case-insensitive):
  +5 per keyword found in the title
  +3 per keyword found in the slug
  +4 per Mews topic tag that contains one of the topic's "tags"
  +1 per keyword hit in the body (capped at BODY_CAP)
  any "exclude" keyword in the title or slug drops the article

Run:
  python filter_mews_articles.py                         # all topics in topics.json
  python filter_mews_articles.py --topic payments        # one topic
  python filter_mews_articles.py --keywords "kiosk,check-in" --name kiosk   # ad hoc
  python filter_mews_articles.py --dry-run               # print ranking only
"""
import argparse
import json
import re
import shutil
from pathlib import Path

W_TITLE, W_SLUG, W_TAG, BODY_CAP = 5, 3, 4, 10
DEFAULT_MIN_SCORE = 5


def read_article(path):
    """Parse the front matter written by fetch_mews_articles.py (JSON values)."""
    text = path.read_text(encoding="utf-8")
    meta, body = {}, text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            for line in text[4:end].splitlines():
                key, sep, val = line.partition(": ")
                if sep:
                    try:
                        meta[key] = json.loads(val)
                    except json.JSONDecodeError:
                        meta[key] = val
            body = text[end + 5:]
    meta.setdefault("slug", path.stem)
    meta.setdefault("title", path.stem)
    meta.setdefault("topics", [])
    meta.setdefault("url", "")
    return meta, body


def kw_pattern(kw):
    # Whole-word match; hyphens and spaces in a keyword match either separator.
    parts = [re.escape(p) for p in re.split(r"[\s\-_]+", kw.strip()) if p]
    return re.compile(r"(?<![\w])" + r"[\s\-_]+".join(parts) + r"(?![\w])", re.I)


def score(meta, body, rule):
    kws = [(k, kw_pattern(k)) for k in rule.get("keywords", [])]
    title, slug = meta["title"], meta["slug"]

    for ex in rule.get("exclude", []):
        p = kw_pattern(ex)
        if p.search(title) or p.search(slug):
            return 0, []

    total, matched = 0, set()
    body_hits = 0
    for k, p in kws:
        if p.search(title):
            total += W_TITLE
            matched.add(k)
        if p.search(slug):
            total += W_SLUG
            matched.add(k)
        n = len(p.findall(body))
        if n:
            body_hits += n
            matched.add(k)
    total += min(body_hits, BODY_CAP)

    tags = [t.lower() for t in rule.get("tags", [])]
    for topic in meta["topics"]:
        if any(t in topic.lower() for t in tags):
            total += W_TAG
            matched.add(f"tag:{topic}")
    return total, sorted(matched)


def write_index(out, name, rule, hits):
    lines = [
        f"# {name}",
        "",
        f"Keywords: {', '.join(rule.get('keywords', [])) or '-'}  ",
        f"Tags: {', '.join(rule.get('tags', [])) or '-'}  ",
        f"{len(hits)} articles, ranked by score.",
        "",
        "| Score | Article | Mews topics | Matched |",
        "|---:|---|---|---|",
    ]
    for s, meta, matched, path in hits:
        title = meta["title"].replace("|", "\\|")
        link = f"[{title}]({path.name})"
        if meta["url"]:
            link += f" ([web]({meta['url']}))"
        lines.append(
            f"| {s} | {link} | {', '.join(meta['topics'])} | {', '.join(matched)} |"
        )
    (out / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--articles", default="data/articles")
    ap.add_argument("--topics", default="topics.json")
    ap.add_argument("--topic", action="append", help="only these topics (repeatable)")
    ap.add_argument("--keywords", help="ad-hoc comma-separated keywords instead of topics.json")
    ap.add_argument("--name", default="adhoc", help="output folder name for --keywords")
    ap.add_argument("--min-score", type=int, help="override every topic's min_score")
    ap.add_argument("--out", default="output")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.keywords:
        topics = {args.name: {"keywords": [k.strip() for k in args.keywords.split(",") if k.strip()], "min_score": 3}}
    else:
        topics = json.loads(Path(args.topics).read_text(encoding="utf-8"))
        if args.topic:
            missing = set(args.topic) - topics.keys()
            if missing:
                ap.error(f"unknown topic(s): {', '.join(sorted(missing))}")
            topics = {k: v for k, v in topics.items() if k in args.topic}

    files = sorted(Path(args.articles).glob("*.md"))
    if not files:
        ap.error(f"no articles in {args.articles} - run fetch_mews_articles.py first")
    articles = [(read_article(f), f) for f in files]
    print(f"{len(articles)} articles, {len(topics)} topic(s)\n")

    for name, rule in topics.items():
        min_score = args.min_score if args.min_score is not None else rule.get("min_score", DEFAULT_MIN_SCORE)
        hits = []
        for (meta, body), path in articles:
            s, matched = score(meta, body, rule)
            if s >= min_score:
                hits.append((s, meta, matched, path))
        hits.sort(key=lambda h: (-h[0], h[1]["title"].lower()))

        print(f"== {name}: {len(hits)} articles (min score {min_score})")
        for s, meta, matched, _ in hits[: 15 if not args.dry_run else None]:
            print(f"  {s:>3}  {meta['title']}  [{', '.join(matched)}]")
        if not args.dry_run and len(hits) > 15:
            print(f"  ... {len(hits) - 15} more in {args.out}/{name}/index.md")

        if not args.dry_run:
            out = Path(args.out) / name
            if out.exists():
                shutil.rmtree(out)  # keep output in sync with the current rules
            out.mkdir(parents=True)
            for _, _, _, path in hits:
                shutil.copy2(path, out / path.name)
            write_index(out, name, rule, hits)
        print()


if __name__ == "__main__":
    main()
