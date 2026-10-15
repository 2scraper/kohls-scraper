"""Assertions for the canary run, kept out of the workflow so they can be run
(and tested) locally against a run's artefacts.

    python .github/canary_assert.py <out-prefix> <exit-code>

Thresholds test the PARSER, not the catalogue: the expected row count and
page count come from the listing's own arithmetic in the sidecar
(`total_results`, `pages_available`, `per_page`), so a brand assortment
growing or shrinking does not turn the canary red. Exit 3 (refused), exit 5
(never obtained) and zero rows are all failures.
"""
import json
import os
import sys

PAGES_ASKED = 3
CORE_FIELDS = ("sku", "url", "title", "price", "currency")


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def checks(prefix, rc):
    meta = load(prefix + ".meta.json", {})
    rows = load(prefix + ".json", [])
    listing = meta.get("listing") or {}
    available = listing.get("pages_available") or PAGES_ASKED
    per_page = listing.get("per_page") or 0
    total = listing.get("total_results") or 0
    pages = min(PAGES_ASKED, available)
    expected = min(total, pages * per_page) if total and per_page else 0
    out = {
        "exit 0 (3 = refused, 5 = never obtained, 4 = no rows)": rc == 0,
        "status complete": meta.get("status") == "complete",
        "pages completed = min(%d, pages_available)" % PAGES_ASKED:
            meta.get("pages_completed") == pages,
        "rows present": bool(rows),
        "the listing states its size": expected > 0,
        # 95%: a sorted listing can still lose a few rows to a product going
        # out of stock between page fetches; a parser regression loses far more.
        "rows >= 95% of what the listing states for those pages":
            expected > 0 and len(rows) >= 0.95 * expected,
    }
    for name in CORE_FIELDS:
        out["every row has %s" % name] = bool(rows) and all(
            r.get(name) not in (None, "") for r in rows)
    out["page+position unique"] = bool(rows) and len(
        {(r.get("page"), r.get("position")) for r in rows}) == len(rows)
    out["no original_price at or below its price"] = all(
        r.get("original_price") is None or r.get("price") is None
        or r["original_price"] > r["price"] for r in rows)
    return out


def main(argv):
    prefix = argv[1]
    try:
        rc = int(argv[2])
    except (IndexError, ValueError):
        rc = 1
    results = checks(prefix, rc)
    for name, ok in results.items():
        print(("ok     " if ok else "FAILED ") + name)
    if rc == 3:
        print("::error::exit 3 — kohls.com refused the run. The endpoint's exit "
              "may be scored, or the profile is not the US one.")
    elif rc == 5:
        print("::error::exit 5 — the page was never obtained, or was served and "
              "parsed to nothing. A 401 in the log means the profile expired: "
              "refresh KOHLS_CDP_ENDPOINT.")
    elif rc == 4:
        print("::error::exit 4 — zero rows from a listing that should hold products.")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
