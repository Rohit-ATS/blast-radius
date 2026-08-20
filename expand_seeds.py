"""Widen the crawl frontier.

BFS from 145 hand-picked seeds converges fast: popular packages depend on each
other, so after ~3k packages the queue drains and the graph stops growing. That
is a property of the seed set, not of npm.

This pulls package names straight from the registry's search API (paged,
popularity-ordered) and writes them to a seeds file. ingest.py merges any seed
it has not visited into the resume queue, so re-running the crawler with this
file as --seeds picks up where it left off and keeps going.

Run:  py expand_seeds.py --out seeds_expanded.txt --target 60000
"""

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

SEARCH = "https://registry.npmjs.org/-/v1/search"

# `text` must be 2-64 characters — single letters are rejected outright
# (ERR_TEXT_LENGTH), which is what silently capped the first version of this
# script at 2.8k names. Results come back popularity-ordered, so paging a broad
# term is a better frontier than enumerating _all_docs, which is lexical and
# starts with several thousand spam packages.
KEYWORDS = [
    "cli", "react", "test", "build", "util", "types", "server", "parser",
    "babel", "webpack", "eslint", "css", "node", "http", "stream", "async",
    "json", "date", "vue", "typescript", "logger", "config", "promise",
    "validation", "database", "aws", "graphql", "auth", "crypto", "markdown",
    "template", "router", "bundler", "lint", "polyfill", "svelte", "angular",
]
PAIRS = ["ab", "ac", "ad", "al", "am", "an", "ar", "as", "at", "au", "ba", "be",
         "bo", "ca", "ch", "co", "da", "de", "di", "do", "el", "en", "er", "es",
         "ex", "fi", "fo", "ge", "gr", "ha", "he", "in", "is", "it", "js", "la",
         "li", "lo", "ma", "me", "mi", "mo", "na", "ne", "no", "ob", "on", "op",
         "pa", "pe", "pl", "po", "pr", "re", "ro", "sa", "se", "sh", "si", "so",
         "st", "su", "ta", "te", "th", "ti", "to", "tr", "ty", "un", "up", "ur",
         "us", "va", "ve", "vi", "we", "wi", "wr", "xm", "ya", "ze"]
TERMS = [f"keywords:{k}" for k in KEYWORDS] + KEYWORDS + PAIRS


def page(session, text, frm, size=250):
    try:
        r = session.get(SEARCH, params={"text": text, "size": size, "from": frm},
                        timeout=30)
        if r.status_code != 200:
            return []
        return [o["package"]["name"] for o in r.json().get("objects", [])
                if o.get("package", {}).get("name")]
    except Exception:
        return []


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="seeds_expanded.txt")
    p.add_argument("--target", type=int, default=60000)
    p.add_argument("--base", default="seeds.txt")
    p.add_argument("--pages", type=int, default=16,
                   help="pages of 250 per term")
    p.add_argument("--concurrency", type=int, default=12)
    args = p.parse_args()

    session = requests.Session()
    session.headers.update({"User-Agent": "blast-radius-hackhydra/0.1"})

    names: dict[str, None] = {}
    try:
        for line in open(args.base, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                names[line] = None
    except FileNotFoundError:
        pass

    started = time.time()
    lock = threading.Lock()

    def harvest(term):
        got_any = 0
        for frm in range(0, args.pages * 250, 250):
            got = page(session, term, frm)
            if not got:
                break
            with lock:
                for n in got:
                    names[n] = None
                got_any += len(got)
                total = len(names)
            if total >= args.target:
                break
        print(f"[seeds] {term:<22} +{got_any:<5} total {len(names):>6} "
              f"({time.time() - started:.0f}s)", flush=True)

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        list(pool.map(harvest, TERMS))

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("# Package names pulled from the npm search API by expand_seeds.py.\n")
        for n in names:
            f.write(n + "\n")
    print(f"[seeds] wrote {len(names)} names to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
