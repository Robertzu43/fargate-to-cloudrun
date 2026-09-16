#!/usr/bin/env python3
"""Maintainer tool. Re-fetch every page cited in rules.json and check each quoted sentence still exists.

Usage:
  docdrift.py [--rules references/rules.json] [--write]

Exits 1 and lists the rule ids whose quote is gone. With --write, sets "stale": true on those rows
(and false on the rest) in place. Never run by the skill during the guided workflow.
"""
import argparse
import html
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES = os.path.join(HERE, "..", "references", "rules.json")
_CACHE = {}


def norm(t):
    t = html.unescape(t).replace("‘", "'").replace("’", "'")
    t = t.replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", t).strip()


def page_text(url):
    if url not in _CACHE:
        req = urllib.request.Request(url, headers={"User-Agent": "fargate-to-cloudrun docdrift"})
        raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
        _CACHE[url] = norm(re.sub(r"<[^>]+>", "", raw))
    return _CACHE[url]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rules", default=DEFAULT_RULES)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    with open(a.rules) as fh:
        doc = json.load(fh)
    stale = []
    for r in doc["rules"]:
        try:
            ok = norm(r["quote"]) in page_text(r["url"])
        except Exception as e:  # network or HTTP error counts as not verified
            print(f"{r['id']}: fetch failed: {e}")
            ok = False
        if not ok:
            stale.append(r["id"])
    if a.write:
        for r in doc["rules"]:
            r["stale"] = r["id"] in stale
        with open(a.rules, "w") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    if stale:
        print("STALE:", *stale)
        sys.exit(1)
    print(f"all {len(doc['rules'])} citations present")


if __name__ == "__main__":
    main()
