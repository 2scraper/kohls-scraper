#!/usr/bin/env python3
"""
make_fixtures.py
----------------
Builds `fixtures_generated.json` — the offline suite's fixtures — from raw
kohls.com captures, and refuses to write it unless every trimmed fixture
parses to exactly what its untrimmed original parses to.

The raw captures live OUTSIDE this repository on purpose (default
`../captures/`, override with --captures). A real listing page is 1-6 MB
and carries the session that fetched it: sponsored-placement trackers
(`impressionTracker`, `clickTracker`, `requestId`), a per-request relevancy
token, Akamai's challenge-script paths. None of that is ours to publish, and
none of it is needed to test a parser — which needs the STRUCTURE.

    python3 make_fixtures.py                       # ../captures -> fixtures_generated.json
    python3 make_fixtures.py --captures DIR --check  # rebuild and compare, write nothing

What a fixture is
-----------------
A listing fixture is rebuilt from its capture, not cut from it:

  * the head keeps the page's JSON-LD block and one design-system <link> —
    the positive "built from Kohl's assets" signal — plus, on pages fetched
    over the Scraping Browser API, the extension's injected
    `chrome-extension://` scripts VERBATIM, because the marker set has to be
    proven to score zero against them;
  * the catalogue island keeps its own encoding, with `products` cut to a
    chosen subset (sponsored entries included) and the 245 KB of filter
    dimensions dropped;
  * the body keeps the chosen products' rendered tiles in their grid
    container, and one tile of the recommendation carousel below it, so the
    fallback path's grid scoping is tested against the real thing.

Verification, before anything is written:

  1. the catalogue path on the fixture returns, for the kept products,
     rows identical to the catalogue path on the full capture;
  2. the URL-pattern fallback (island removed) likewise, on both;
  3. the page state is the same on both;
  4. the fixture carries none of the scrubbed shapes (see SCRUB_CHECKS).

A detail fixture keeps the product model's fields this parser reads, with
the SKU list cut to a subset that holds BOTH availability values, and is
checked the same way on the fields that do not depend on the SKU count.
"""

import argparse
import html as html_lib
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path

from bs4 import BeautifulSoup

import product_parser as pp

REPO = Path(__file__).resolve().parent
OUT = REPO / "fixtures_generated.json"
DEFAULT_CAPTURES = REPO.parent / "captures"

# Replaced wherever they occur in a kept island. Values, not keys, so the
# STRUCTURE the parser reads is untouched.
SCRUB_KEYS = ("impressionTracker", "clickTracker", "opportunityTracker",
              "requestId", "relevancyAlgorithm", "campaignId", "providerId")
SCRUBBED = "SCRUBBED-by-make_fixtures"

# Shapes that must not survive into a fixture, checked on the OUTPUT.
SCRUB_CHECKS = {
    "koddi tracker": re.compile(r"koddi\.io/event-collection"),
    "email address": re.compile(r"[\w.+-]+@[\w-]+\.(?:com|net|org)\b"),
    "32-hex string": re.compile(r"\b[0-9a-f]{32}\b"),
    "akamai script path": re.compile(r'src="/[A-Za-z0-9]{3,6}/[A-Za-z0-9]{3,6}/'),
}

# (name, capture file, URL it was fetched from, how many products to keep,
#  extra webIDs to force in because a check needs that exact row)
LISTINGS = [
    ("listing_sort4_p1", "listing_womens_sort4_p1.html",
     "https://www.kohls.com/catalog/womens-clothing.jsp?CN=Gender:Womens+Department:Clothing&S=4",
     10, ["c2435951"]),             # an outfit "collection" entry
    ("listing_featured_p2", "listing_womens_p2.html",
     "https://www.kohls.com/catalog/womens-clothing.jsp?CN=Gender:Womens+Department:Clothing&WS=120&PPP=120",
     8, []),
    ("search_crocs_p1", "search_crocs_p1.html",
     "https://www.kohls.com/search.jsp?submit-search=web-regular&search=crocs",
     6, ["7862957"]),              # a tile whose regular price reads "Orig."
    ("search_coffee_p2", "search_coffee_maker_p2.html",
     "https://www.kohls.com/search.jsp?submit-search=web-regular&search=coffee+maker&WS=120",
     6, ["6137474"]),              # a tile whose regular price has no label
    ("listing_cuisinart_p1", "listing_brand_cuisinart_p1.html",
     "https://www.kohls.com/catalog/cuisinart.jsp?CN=Brand:Cuisinart",
     8, []),
]
DETAILS = [
    ("detail_croft_tee", "detail_croft_tee.html",
     "https://www.kohls.com/product/prd-3500577/womens-croft-barrow-essential-crewneck-tee.jsp", 12),
    ("detail_cuisinart", "detail_cuisinart_coffee.html",
     "https://www.kohls.com/product/prd-2977125/cuisinart-14-cup-programmable-coffee-maker.jsp", 3),
]
# Small enough to keep whole; scrubbed of challenge-script paths only.
VERBATIM = [
    ("deny_raw_entities", "deny_raw_entities_http.html", "https://www.kohls.com/catalog/womens-clothing.jsp"),
    ("deny_dom_403", "deny_dom_403_headless.html", "https://www.kohls.com/catalog/womens-clothing.jsp?CN=Gender:Womens+Department:Clothing"),
    ("deny_dom_with_script", "deny_dom_with_script.html", "https://www.kohls.com/catalog/womens-clothing.jsp"),
    ("challenge_sec_cpt", "challenge_sec_cpt.html", "https://www.kohls.com/catalog/womens-clothing.jsp"),
]
END_PAGE = ("end_page_not_available", "search_crocs_past_end_404.html",
            "https://www.kohls.com/catalog/page_not_available.jsp")

# What verification compares. `scraped_at` is a timestamp.
_VOLATILE = {"scraped_at"}


def _row(r) -> dict:
    return {k: v for k, v in asdict(r).items() if k not in _VOLATILE}


def _scrub(value):
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
        kind, inner = value
        if kind == 0 and isinstance(inner, dict):
            return [0, {k: ([0, SCRUBBED] if k in SCRUB_KEYS and v != [0, None] else _scrub(v))
                        for k, v in inner.items()}]
        if kind == 1 and isinstance(inner, list):
            return [1, [_scrub(x) for x in inner]]
        return value
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    return value


def _catalog_island(html: str):
    """(opening tag, decoded raw props) of the catalogue island."""
    for tag in pp._ISLAND_RE.findall(html):
        m = pp._ATTR_RE["props"].search(tag)
        if m and "catalogData" in m.group(1):
            return tag, json.loads(html_lib.unescape(m.group(1)))
    raise SystemExit("no catalogue island in the capture")


def _encode_attr(obj) -> str:
    return html_lib.escape(json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
                           quote=True)


def _head(soup) -> str:
    parts = []
    title = soup.find("title")
    if title:
        parts.append(str(title))
    # One element carrying a Kohl's asset host — whatever kind of element the
    # page happens to carry it on. The 404 page has no design-system <link>
    # but does load its images from media.kohlsimg.com; keeping only <link>s
    # produced an end-page fixture with NO positive signal at all, and the
    # verification below missed it because it judged the page by its
    # page_not_available URL, which short-circuits before the asset test.
    asset = next((el for el in soup.find_all(["link", "img", "script"])
                  if any(m in (el.get("href") or el.get("src") or "")
                         for m in pp.SITE_ASSET_MARKERS)), None)
    if asset is not None:
        parts.append(str(asset) if asset.name != "script"
                     else '<script src="%s"></script>' % asset["src"])
    for s in soup.find_all("script", type="application/ld+json"):
        parts.append(str(s))
    # The Scraping Browser's own injected scripts, verbatim: the detector has
    # to be shown to ignore them, and a fixture without them proves nothing.
    for s in soup.find_all("script", src=True):
        if s["src"].startswith(("chrome-extension://", "moz-extension://")):
            parts.append(str(s))
    return "".join(parts)


def _tiles(soup, keep_ids):
    """Grid tiles for `keep_ids`, in page order, plus one carousel tile."""
    tiles = []
    for a in soup.select(pp.SELECTORS["item_link"]):
        sku = pp.sku_from_url(a.get("href"))
        if sku:
            tiles.append((sku, pp._tile_scope(a)))
    counts = {}
    for _, t in tiles:
        counts[id(t.parent)] = counts.get(id(t.parent), 0) + 1
    grid = max(counts, key=counts.get)
    grid_tiles, seen, carousel = [], set(), None
    for sku, t in tiles:
        if id(t.parent) == grid:
            if sku in keep_ids and sku not in seen:
                seen.add(sku)
                grid_tiles.append(str(t))
        elif carousel is None and sku not in keep_ids:
            carousel = str(t)
    return grid_tiles, carousel


def build_listing(capture: str, url: str, n: int, force):
    soup = BeautifulSoup(capture, "html.parser")
    tag, props = _catalog_island(capture)
    cat = props["catalogData"][1]
    products = cat["products"][1]
    organic = [p for p in products if not p[1].get("sponsoredData", [0, None])[1]
               and p[1].get("prodType", [0, None])[1] == "product"]
    others = [p for p in products if not p[1].get("sponsoredData", [0, None])[1]
              and p[1].get("prodType", [0, None])[1] != "product"]
    sponsored = [p for p in products if p[1].get("sponsoredData", [0, None])[1]]
    ids = lambda p: p[1]["webID"][1]
    keep = organic[:n] + [p for p in organic + others
                          if ids(p) in force and p not in organic[:n]]
    keep_ids = {ids(p) for p in keep}
    # One sponsored entry, placed where the site placed it, so the filter and
    # the emitted-row positions are tested on a real slot.
    chosen = [p for p in products if p in keep or (sponsored and p is sponsored[0])]
    cat = dict(cat)
    cat["products"] = [1, chosen]
    cat["dimensions"] = [1, []]
    cat["metaInfo"] = [0, None]
    props = dict(props)
    props["catalogData"] = [0, cat]
    props = _scrub(props)
    new_tag = pp._ATTR_RE["props"].sub(lambda m: f'props="{_encode_attr(props)}"', tag, count=1)
    grid, carousel = _tiles(soup, keep_ids)
    body = ('<div class="grid">' + "".join(grid) + "</div>"
            + ('<div class="carousel">' + carousel + "</div>" if carousel else ""))
    return ("<!DOCTYPE html><html><head>" + _head(soup) + "</head><body>"
            + new_tag + "</astro-island>" + body + "</body></html>"), keep_ids


def _no_island(html: str) -> str:
    return pp._ISLAND_RE.sub("<div>", html)


def verify_listing(name, full, trimmed, url, keep_ids, problems):
    if pp.detect_page_state(full, url=url) != pp.detect_page_state(trimmed, url=url):
        problems.append(f"{name}: page state differs after trimming")
    for label, a, b in (("catalogue", full, trimmed),
                        ("fallback", _no_island(full), _no_island(trimmed))):
        want = {r.sku: _row(r) for r in pp.parse_products(a, url) if r.sku in keep_ids}
        got = {r.sku: _row(r) for r in pp.parse_products(b, url)}
        # Positions renumber on a subset; compare everything else exactly.
        strip = lambda d: {k: v for k, v in d.items() if k != "position"}
        if set(got) != set(want):
            problems.append(f"{name} [{label}]: rows {sorted(set(got) ^ set(want))[:5]} "
                            f"differ between the fixture and its capture")
            continue
        for sku in want:
            if strip(got[sku]) != strip(want[sku]):
                diff = {k: (want[sku][k], got[sku][k]) for k in want[sku]
                        if k != "position" and want[sku][k] != got[sku].get(k)}
                problems.append(f"{name} [{label}] sku {sku}: {diff}")


_DETAIL_KEYS = ("webID", "productTitle", "productStatus", "seoURL", "brand",
                "avgRating", "ratingCount", "price", "images", "breadcrumbs",
                "SKUS")
_SKU_KEYS = ("skuCode", "color", "size", "availability")


def build_detail(capture: str, n_skus: int):
    soup = BeautifulSoup(capture, "html.parser")
    for tag in pp._ISLAND_RE.findall(capture):
        comp = pp._ATTR_RE["component"].search(tag)
        if comp and "ProductDetails" in comp.group(1):
            break
    else:
        raise SystemExit("no ProductDetails island in the capture")
    raw = json.loads(html_lib.unescape(pp._ATTR_RE["props"].search(tag).group(1)))
    product = raw["product"][1]
    kept = {k: product[k] for k in _DETAIL_KEYS if k in product}
    skus = kept["SKUS"][1]
    ins = [s for s in skus if s[1]["availability"][1] == "In Stock"]
    outs = [s for s in skus if s[1]["availability"][1] != "In Stock"]
    subset = (ins[: max(1, n_skus // 2)] + outs[: n_skus - max(1, n_skus // 2)]) or skus[:n_skus]
    kept["SKUS"] = [1, [[0, {k: s[1][k] for k in _SKU_KEYS if k in s[1]}] for s in subset]]
    kept["price"] = [0, {k: v for k, v in kept["price"][1].items()
                         if k in ("salePrice", "regularPrice", "isSuppressed")}]
    new_props = {"product": [0, kept]}
    new_tag = pp._ATTR_RE["props"].sub(lambda m: f'props="{_encode_attr(new_props)}"', tag, count=1)
    ld = soup.find_all("script", type="application/ld+json")
    for s in ld:
        data = json.loads(s.string)
        if data.get("@type") == "Product":
            data["offers"] = data.get("offers", [])[:3]
            data.pop("description", None)
            s.string = json.dumps(data, ensure_ascii=False)
    head = _head(soup)
    return "<!DOCTYPE html><html><head>" + head + "</head><body>" + new_tag + "</astro-island></body></html>"


def verify_detail(name, full, trimmed, url, problems):
    a, b = pp.parse_product_detail(full, url), pp.parse_product_detail(trimmed, url)
    if a is None or b is None:
        problems.append(f"{name}: parsed to None")
        return
    ra, rb = _row(a), _row(b)
    for k in ra:
        if k in ("skus_total", "skus_in_stock"):
            continue  # the SKU list was cut on purpose
        if ra[k] != rb[k]:
            problems.append(f"{name}: {k} {ra[k]!r} -> {rb[k]!r} after trimming")


def build(captures: Path):
    fixtures, problems = {}, []

    def read(fn):
        return (captures / fn).read_text(encoding="utf-8", errors="replace")

    for name, fn, url, n, force in LISTINGS:
        full = read(fn)
        trimmed, keep_ids = build_listing(full, url, n, set(force))
        verify_listing(name, full, trimmed, url, keep_ids, problems)
        fixtures[name] = {"url": url, "html": trimmed, "source": fn}
    for name, fn, url, n in DETAILS:
        full = read(fn)
        trimmed = build_detail(full, n)
        verify_detail(name, full, trimmed, url, problems)
        fixtures[name] = {"url": url, "html": trimmed, "source": fn}
    for name, fn, url in VERBATIM:
        full = read(fn)
        # Akamai's challenge scripts are served from per-challenge paths; the
        # paths are session material, the markup around them is not.
        body = re.sub(r'src="/[A-Za-z0-9]{3,6}/[^"]+"', 'src="/SCRUBBED-akamai-script"', full)
        body = re.sub(r"(https://www\.kohls\.com/public/)[0-9a-f]+", r"\1SCRUBBED", body)
        if pp.detect_page_state(full, url=url) != pp.detect_page_state(body, url=url):
            problems.append(f"{name}: page state changed by scrubbing")
        fixtures[name] = {"url": url, "html": body, "source": fn}
    # Chromium's own network-error page: 186 KB whose <title> is the SITE's
    # hostname and which carries no Kohl's asset — the case the structural
    # "built from Kohl's assets" test exists for. Inline styles and scripts
    # dropped; the text and the error code kept.
    full = read("chromium_error_proxy_failed.html")
    soup = BeautifulSoup(full, "html.parser")
    for t in soup.find_all(["style", "script"]):
        t.decompose()
    err = str(soup)
    url = "https://www.kohls.com/catalog/cuisinart.jsp?CN=Brand:Cuisinart"
    if pp.detect_page_state(full, url=url) != pp.detect_page_state(err, url=url):
        problems.append("chromium_error_page: page state differs after trimming")
    fixtures["chromium_error_page"] = {"url": url, "html": err,
                                       "source": "chromium_error_proxy_failed.html",
                                       "note": "inline <style>/<script> removed"}

    # /signin, fetched over the Scraping Browser in 2026-08: the site's own
    # reCAPTCHA v3, plus the extension's injected hunters. Kept: every script
    # that mentions recaptcha, every element with a data-sitekey, and the
    # head; everything else dropped.
    from captcha_solver import detect_recaptcha_v3
    full = read("signin_recaptcha_2026-08-05.html")
    soup = BeautifulSoup(full, "html.parser")
    # Extension scripts are already in _head(); these are the SITE's own.
    keep = [str(t) for t in soup.find_all("script")
            if ("recaptcha" in (t.string or "") or "recaptcha" in (t.get("src") or ""))
            and not (t.get("src") or "").startswith(("chrome-extension://", "moz-extension://"))]
    keep += [str(t) for t in soup.select("[data-sitekey]")]
    signin = ("<!DOCTYPE html><html><head>" + _head(soup) + "</head><body>"
              + "".join(keep) + "</body></html>")
    url = "https://www.kohls.com/myaccount/kohls_login.jsp"
    before, after = detect_recaptcha_v3(full, url), detect_recaptcha_v3(signin, url)
    if (before and (before.kind, before.sitekey, before.action)) != (after and (after.kind, after.sitekey, after.action)):
        problems.append("signin_recaptcha: detector result differs after trimming")
    fixtures["signin_recaptcha"] = {"url": url, "html": signin,
                                    "source": "signin_recaptcha_2026-08-05.html",
                                    "note": "recaptcha scripts, data-sitekey elements and head only"}

    name, fn, url = END_PAGE
    full = read(fn)
    soup = BeautifulSoup(full, "html.parser")
    # The page's OWN "not available" container, verbatim — it is what the
    # classifier decides on — plus the one product link the real page
    # carries (a recommendation), so a classifier that missed the container
    # would visibly fall through to "content" here as it did on the capture.
    container = soup.find(id="page_not_avail")
    stray = next((a for a in soup.select(pp.SELECTORS["item_link"])
                  if pp.sku_from_url(a.get("href"))), None)
    end_html = ("<!DOCTYPE html><html><head>" + _head(soup) + "</head><body>"
                + (str(container) if container else "")
                + (str(stray) if stray else "") + "</body></html>")
    if container is None:
        problems.append(f"{name}: the capture has no page_not_avail container")
    # Judged in every combination a driver can hand over — with and without
    # a status, by the landing URL and by the URL that was ASKED for — so no
    # single signal can short-circuit the check into passing a fixture that
    # lost the one that matters.
    asked = "https://www.kohls.com/search.jsp?submit-search=web-regular&search=crocs&WS=120"
    for u in (url, asked):
        for st in (404, None):
            if (pp.detect_page_state(full, status=st, url=u) != "end"
                    or pp.detect_page_state(end_html, status=st, url=u) != "end"):
                problems.append(f"{name}: not 'end' with status={st}, url={u}")
    fixtures[name] = {"url": url, "html": end_html, "source": fn,
                      "note": "the page's own not-available container and one of its product links, around its head"}

    for fname, fx in fixtures.items():
        for label, rx in SCRUB_CHECKS.items():
            if rx.search(fx["html"]):
                problems.append(f"{fname}: still contains a {label}")
    return fixtures, problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--captures", type=Path, default=DEFAULT_CAPTURES)
    ap.add_argument("--check", action="store_true",
                    help="Rebuild and compare with the committed file; write nothing.")
    args = ap.parse_args()
    if not args.captures.is_dir():
        print(f"no captures directory at {args.captures} — the raw captures are "
              f"deliberately not in the repo; see this file's docstring.")
        return 2
    fixtures, problems = build(args.captures)
    if problems:
        print("REFUSING to write fixtures:")
        for p in problems:
            print("  -", p)
        return 1
    payload = {
        "_about": ("Generated by make_fixtures.py from live kohls.com captures taken "
                   "2026-09-25 through the 2Captcha Scraping Browser API (the deny "
                   "and challenge pages: local Chromium, and the 2026-08 prototype). "
                   "NOT verbatim: listing and detail pages are rebuilt around a "
                   "subset of their own data, with sponsored-placement trackers and "
                   "the relevancy token replaced by 'SCRUBBED-by-make_fixtures'. "
                   "Each trimmed fixture was verified to parse identically to its "
                   "full capture before this file was written."),
        "fixtures": fixtures,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if args.check:
        same = OUT.is_file() and OUT.read_text(encoding="utf-8") == text
        print("fixtures_generated.json is up to date" if same else "fixtures_generated.json differs")
        return 0 if same else 1
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.name}: {len(fixtures)} fixtures, {len(text) // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
