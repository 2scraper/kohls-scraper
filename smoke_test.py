#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke_test.py
-------------
Zero-network, zero-browser sanity check for kohls-scraper.

    python3 smoke_test.py

One file of plain functions — no pytest, no conftest. tests/test_smoke.py
wraps it as a single pytest test. The fixtures are real kohls.com captures,
trimmed and scrubbed by make_fixtures.py into fixtures_generated.json (the
raw captures are deliberately not in the repo; see that script).

It must pass with NO engine library installed, so every engine import is
guarded and a skip is REPORTED; CI's engine-smoke job fails on an
unexpected one.

Every check that matters pins a VALUE read off a real capture: a column can
be 100% populated and entirely wrong, and coverage says nothing about that.

Exits non-zero on any failure.
"""

import ast
import builtins
import importlib
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import fields, asdict

import browser_bridge
from browser_bridge import (DriverError, PageOutcome, fetch_pages_concurrently,
                            build_parser, validate_args, mask_credentials,
                            proxy_failure_in)
from captcha_solver import detect_recaptcha_v3
from diff_runs import diff_products
import env_config
import page_flow
from output_writer import (Product, save, finish_run, write_csv, dedupe_by_key,
                           EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_PARTIAL,
                           EXIT_FETCH_FAILED, COMPLETE_STOP_REASONS)
import product_parser as pp
from proxy_pool import ProxyPool, mask, to_playwright, split_credentials

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
FIXTURES = json.load(open(os.path.join(REPO_ROOT, "fixtures_generated.json"),
                          encoding="utf-8"))["fixtures"]

_failures = []


def check(label, condition):
    """Print and record one check; returns the condition."""
    if condition:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s" % label)
        _failures.append(label)
    return bool(condition)


def group(title):
    print("\n== %s" % title)


def fx(name):
    return FIXTURES[name]["html"], FIXTURES[name]["url"]


def _raises_exit(fn):
    try:
        fn()
    except SystemExit as e:
        return e.code != 0
    return False


def rows_of(name):
    html, url = fx(name)
    return pp.parse_products(html, url)


def by_sku(rows):
    return {r.sku: r for r in rows}


def quiet(fn, *a, **kw):
    with io.StringIO() as buf, redirect_stdout(buf), redirect_stderr(io.StringIO()):
        return fn(*a, **kw)


# ---------------------------------------------------------------------------
# Listing values, pinned
# ---------------------------------------------------------------------------
def test_listing_values():
    group("listing rows: values pinned from real captures")
    ok = True
    r = by_sku(rows_of("listing_sort4_p1"))
    a = r.get("7580242")
    ok &= check("sort4 p1: 10 organic rows", len(r) == 10)
    ok &= check("a clearance tee: price 2.0, regular 20.0, 90% off",
                a and (a.price, a.original_price, a.discount_pct) == (2.0, 20.0, 90.0))
    ok &= check("...its price label is the site's own ('clearance')",
                a and a.price_label == "clearance")
    ok &= check("...rating 4.1 from 9 reviews",
                a and (a.rating, a.review_count) == (4.1, 9))
    ok &= check("...currency from the page's JSON-LD", a and a.currency == "USD")
    ok &= check("...price_source says the catalogue island", a and a.price_source == "catalog")
    ok &= check("...the ordering the SITE reports is a column",
                a and a.sort == "Price Low-High")
    ok &= check("...url is the product's own canonical path",
                a and a.url.startswith("https://www.kohls.com/product/prd-7580242/"))
    ok &= check("...sku is the webID, which is the number in the URL",
                a and pp.sku_from_url(a.url) == a.sku)
    ok &= check("...category defaults to the URL slug",
                a and a.category == "womens-clothing")
    ok &= check("...brand is null on a listing row (the payload has none)",
                all(x.brand is None for x in r.values()))
    # Zero is not a rating: the payload writes an unreviewed product as
    # avgRating 0 / count 0, and both columns go null together.
    z = r.get("8271342")
    ok &= check("an unreviewed product has rating AND review_count null",
                z and z.rating is None and z.review_count is None)
    ok &= check("no row carries a zero rating or a zero review count",
                all(x.rating != 0 and x.review_count != 0 for x in r.values()))

    r2 = by_sku(rows_of("listing_featured_p2"))
    b = r2.get("6867329")
    ok &= check("a range product: price 17.99-23.99, regular 29.99",
                b and (b.price, b.price_max, b.original_price, b.original_price_max)
                == (17.99, 23.99, 29.99, None))
    ok &= check("...discount computed at the low ends (40.0), not read from a badge",
                b and b.discount_pct == 40.0)
    ok &= check("...page is the page the SERVER served (2)", b and b.page == 2)
    c = r2.get("7651770")
    ok &= check("a regular-price product: no original, no discount, no label",
                c and (c.price, c.original_price, c.discount_pct, c.price_label)
                == (44.99, None, None, None))
    ok &= check("no row has an original_price at or below its price",
                all(x.original_price is None or x.original_price > x.price
                    for n in ("listing_sort4_p1", "listing_featured_p2",
                              "search_crocs_p1", "search_coffee_p2",
                              "listing_cuisinart_p1")
                    for x in rows_of(n)))

    r3 = by_sku(rows_of("search_crocs_p1"))
    d = r3.get("6602998")
    ok &= check("both ranges carried: 37.49-54.99 against 49.99-54.99",
                d and (d.price, d.price_max, d.original_price, d.original_price_max)
                == (37.49, 54.99, 49.99, 54.99))
    ok &= check("a search row has no category unless one is passed",
                d and d.category is None)
    ok &= check("--category fills it on a search run",
                pp.parse_products(*fx("search_crocs_p1"), category="crocs")[0].category == "crocs")
    return ok


def test_sponsored_and_positions():
    group("sponsored placements are dropped; positions count emitted rows")
    ok = True
    html, url = fx("listing_sort4_p1")
    data = pp.catalog_data(html)
    sponsored = [p for p in data["products"] if p.get("sponsoredData")]
    ok &= check("the fixture carries a real sponsored entry", len(sponsored) == 1)
    # The check that tests the FILTER, not the id recovery behind it: this
    # sponsored entry wears a product-shaped URL and a numeric webID, so if
    # the sponsored filter were removed it WOULD become a row. A sibling
    # repo's version of this check passed with its filter disabled, because
    # its sponsored entries had no recoverable id and died later anyway.
    s = sponsored[0]
    ok &= check("...and it has a product-shaped URL (so the filter is what drops it)",
                pp.sku_from_url(s.get("seoURL")) == s.get("webID"))
    rows = pp.parse_products(html, url)
    ok &= check("...and it is not in the output", s["webID"] not in by_sku(rows))
    # Two defences drop that entry — the sponsored filter AND the prodType
    # allowlist (a sponsored entry's prodType is empty, 40 of 40 measured) —
    # so the check above passes with EITHER removed. Measured: with the
    # sponsored filter deleted it stayed green. The check that tests the
    # FILTER is a sponsored entry wearing prodType "product".
    tag = pp._ISLAND_RE.search(html).group(0)
    raw = json.loads(__import__("html").unescape(pp._ATTR_RE["props"].search(tag).group(1)))
    for entry in raw["catalogData"][1]["products"][1]:
        if entry[1].get("sponsoredData", [0, None])[1]:
            entry[1]["prodType"] = [0, "product"]
    disguised = html.replace(tag, pp._ATTR_RE["props"].sub(
        lambda m: 'props="%s"' % __import__("html").escape(json.dumps(raw), quote=True), tag))
    ok &= check("a sponsored entry claiming prodType 'product' is still dropped",
                s["webID"] not in by_sku(pp.parse_products(disguised, url)))
    # Outfit "collection" entries: same URL shape, a price that is the range
    # of their member products. Dropped by the prodType allowlist — and the
    # check proves it is the ALLOWLIST doing it, because the catalogue path
    # takes the id from `webID` ("c2435951"), not from the URL.
    coll = [p for p in data["products"] if p.get("prodType") == "collection"]
    ok &= check("the fixture carries a real outfit 'collection' entry",
                coll and coll[0]["webID"] == "c2435951")
    ok &= check("...which is not emitted as a row",
                "c2435951" not in by_sku(rows) and all(r.sku.isdigit() for r in rows))
    ok &= check("positions are 1..N over emitted rows, with no gap where the ad was",
                [r.position for r in rows] == list(range(1, len(rows) + 1)))
    # page + position is the row's address in the listing; worthless if two
    # rows share it (a sibling shipped page=1 on every row of a 2-page run).
    both = rows_of("listing_sort4_p1") + rows_of("listing_featured_p2")
    ok &= check("page+position is unique across two pages",
                len({(r.page, r.position) for r in both}) == len(both))
    return ok


def test_jsonld_is_a_decoy():
    group("JSON-LD is a 15-item decoy; the island is the listing")
    ok = True
    for name in ("listing_sort4_p1", "search_crocs_p1"):
        html, url = fx(name)
        ld_items = sum(len((b.get("mainEntity") or {}).get("itemListElement") or [])
                       for b in pp._ld_blocks(html))
        ok &= check("%s: the JSON-LD ItemList holds 15 items" % name, ld_items == 15)
    # The pinned fact the whole design rests on, measured on the full captures
    # before trimming: 15 JSON-LD items against 120 payload products per page.
    # A JSON-LD-first parser would return 15 rows and report success.
    ok &= check("rows come from the island, not from JSON-LD",
                all(r.price_source == "catalog" for r in rows_of("listing_sort4_p1")))
    html, url = fx("listing_sort4_p1")
    no_ld = re.sub(r'<script[^>]*application/ld\+json.*?</script>', "", html, flags=re.S)
    rows = pp.parse_products(no_ld, url)
    ok &= check("without JSON-LD the rows are all still there",
                len(rows) == len(rows_of("listing_sort4_p1")))
    ok &= check("...but currency is null — never a defaulted USD",
                all(r.currency is None for r in rows))
    return ok


def test_fallback_path():
    group("URL-pattern fallback, when there is no island")
    ok = True
    for name in ("listing_sort4_p1", "search_crocs_p1", "search_coffee_p2",
                 "listing_cuisinart_p1"):
        html, url = fx(name)
        stripped = pp._ISLAND_RE.sub("<div>", html)
        cat = by_sku(pp.parse_products(html, url))
        fb = pp.parse_products(stripped, url)
        key = lambda x: (x.price, x.price_max, x.original_price,
                         x.original_price_max, x.review_count, x.title)
        ok &= check("%s: every fallback row is a catalogue row" % name,
                    fb and all(x.sku in cat for x in fb))
        ok &= check("%s: fallback prices, counts and titles equal the catalogue's" % name,
                    all(key(x) == key(cat[x.sku]) for x in fb))
        ok &= check("%s: the fallback says so in price_source" % name,
                    all(x.price_source == "dom" for x in fb))
    # The recommendation carousel below the grid has the same URL shape and
    # prices; without grid scoping its products became rows.
    html, url = fx("listing_sort4_p1")
    stripped = pp._ISLAND_RE.sub("<div>", html)
    carousel = re.search(r'<div class="carousel">(.*)</div>', stripped, re.S)
    car_ids = {pp.sku_from_url(h) for h in re.findall(r'href="([^"]+)"', carousel.group(1))} - {None}
    ok &= check("the fixture carries a carousel tile", bool(car_ids))
    ok &= check("...and no carousel product becomes a row",
                not (car_ids & {x.sku for x in pp.parse_products(stripped, url)}))
    crocs = by_sku(pp.parse_products(pp._ISLAND_RE.sub("<div>", fx("search_crocs_p1")[0]),
                                     fx("search_crocs_p1")[1]))
    ok &= check("a regular price labelled 'Orig.' is read (Crocs 7862957: 49.99)",
                crocs.get("7862957") and crocs["7862957"].original_price == 49.99)
    coffee = by_sku(pp.parse_products(pp._ISLAND_RE.sub("<div>", fx("search_coffee_p2")[0]),
                                      fx("search_coffee_p2")[1]))
    ok &= check("an UNLABELLED regular price is read (Zulay 6137474: 1199.99)",
                coffee.get("6137474") and coffee["6137474"].original_price == 1199.99)
    ok &= check("an outfit link (prd-c…) is not a product id",
                pp.sku_from_url("/product/prd-c2435951/womens-croft-barrow-spring-outfit.jsp") is None)
    return ok


def test_detail():
    group("--mode product: the detail page's own model")
    ok = True
    html, url = fx("detail_croft_tee")
    r = pp.parse_product_detail(html, url)
    ok &= check("brand is read (Croft & Barrow)", r and r.brand == "Croft & Barrow")
    ok &= check("price range 6.59-7.99 against regular 11.99, 45% off",
                r and (r.price, r.price_max, r.original_price, r.discount_pct)
                == (6.59, 7.99, 11.99, 45.0))
    ok &= check("SKU stock counts carry both values (6 of 12 in the fixture)",
                r and (r.skus_total, r.skus_in_stock, r.in_stock) == (12, 6, True))
    ok &= check("category is the deepest CATEGORY crumb, not the brand crumb",
                r and r.category == "Tops & Tees")
    ok &= check("sku is the same webID a listing row carries",
                r and r.sku == "3500577" and r.price_source == "product")
    c = pp.parse_product_detail(*fx("detail_cuisinart"))
    ok &= check("a single-price product: 149.99, no range, no original",
                c and (c.price, c.price_max, c.original_price) == (149.99, None, None))
    # The detail page has no catalogue island: the listing parser's primary
    # path finds nothing there (on the full capture its URL fallback reads
    # the recommendation strip — which is why --mode listing refuses a
    # /product/ URL outright).
    ok &= check("a detail page has no listing island", pp.catalog_data(html) is None)
    p = build_parser("t")
    ok &= check("--mode listing refuses a /product/ URL",
                quiet(lambda: _raises_exit(lambda: validate_args(p, p.parse_args(["--url", url])))))
    # And the thinner JSON-LD path, when the island is missing.
    ld = pp.parse_product_detail(pp._ISLAND_RE.sub("<div>", html), url)
    ok &= check("without the island, JSON-LD still gives brand, price and rating",
                ld and ld.brand == "Croft & Barrow" and ld.price is not None
                and ld.price_source == "jsonld" and ld.rating == 4.5)
    return ok


# ---------------------------------------------------------------------------
# Page state
# ---------------------------------------------------------------------------
def test_page_state():
    group("page state: every capture classified as what it is")
    ok = True
    expect = {
        "listing_sort4_p1": "content", "search_crocs_p1": "content",
        "detail_croft_tee": "content",
        "deny_raw_entities": "blocked", "deny_dom_403": "blocked",
        "deny_dom_with_script": "blocked", "challenge_sec_cpt": "challenge",
        "chromium_error_page": "blocked",
    }
    for name, want in expect.items():
        html, url = fx(name)
        ok &= check("%s -> %s" % (name, want), pp.detect_page_state(html, url=url) == want)
    html, url = fx("end_page_not_available")
    asked = "https://www.kohls.com/search.jsp?submit-search=web-regular&search=crocs&WS=120"
    for st in (404, None):
        for u in (url, asked):
            ok &= check("the page-not-available page is the END (status=%s, %s URL)"
                        % (st, "landing" if u == url else "asked"),
                        pp.detect_page_state(html, status=st, url=u) == "end")
    ok &= check("...by its own container, which no served page carries",
                all("page_not_avail" not in v["html"] for k, v in FIXTURES.items()
                    if k != "end_page_not_available"))
    # The deny page in its RAW form entity-escapes its punctuation; a
    # literal marker misses it. Both spellings must be recognised.
    raw, _ = fx("deny_raw_entities")
    ok &= check("the raw deny page really is entity-escaped (the fixture proves the trap)",
                "errors&#46;edgesuite" in raw)
    ok &= check("...and is still named akamai-deny", pp.detect_bot_challenge(raw) == "akamai-deny")
    ok &= check("a deny served under HTTP 200 is still blocked",
                pp.detect_page_state(fx("deny_dom_403")[0], status=200) == "blocked")
    for blank in ("", "<html><head></head><body></body></html>", "   "):
        ok &= check("a blank document (%r) is 'unloaded', not blocked" % blank[:20],
                    pp.detect_page_state(blank, status=200) == "unloaded")
    # Every marker must score ZERO on pages the Scraping Browser fetched —
    # 16 injected extension scripts each — with NO extension-tag strip.
    cdp_pages = [n for n, v in FIXTURES.items() if "chrome-extension://" in v["html"]]
    ok &= check("there are CDP-fetched fixtures to test against (%d)" % len(cdp_pages),
                len(cdp_pages) >= 5)
    for n in cdp_pages:
        if n.startswith("signin"):
            continue
        ok &= check("%s: no challenge marker fires on a served CDP page" % n,
                    pp.detect_bot_challenge(fx(n)[0]) is None)
    ok &= check("the marker set carries neither cf-turnstile nor captcha-widgets",
                not any(m in ("cf-turnstile", "captcha-widgets", "akam", "recaptcha")
                        for ms in pp.BOT_CHALLENGE_MARKERS.values() for m in ms))
    return ok


def test_urls():
    group("URLs: pagination, page kind, category")
    ok = True
    cat = "https://www.kohls.com/catalog/womens-clothing.jsp?CN=Gender:Womens+Department:Clothing"
    ok &= check("page 2 is WS=120 (the grid's own Next button builds this)",
                pp.page_url(cat, 2) == cat + "&WS=120")
    ok &= check("page 1 carries no WS", pp.page_url(cat + "&WS=240", 1) == cat)
    ok &= check("WS is replaced, not duplicated",
                pp.page_url(cat + "&WS=120", 3).count("WS=") == 1
                and pp.page_url(cat + "&WS=120", 3).endswith("WS=240"))
    ok &= check("the CN filter survives unencoded (it IS the category)",
                "CN=Gender:Womens+Department:Clothing" in pp.page_url(cat, 5))
    ok &= check("a caller's own PPP sets the stride",
                pp.page_url(cat + "&PPP=48", 3).endswith("WS=96"))
    ok &= check("page_number_from_url inverts page_url",
                all(pp.page_number_from_url(pp.page_url(cat, n)) == n for n in (1, 2, 7, 1317)))
    ok &= check("listing kinds",
                (pp.listing_kind(cat), pp.listing_kind("https://www.kohls.com/search.jsp?search=x"),
                 pp.listing_kind("https://www.kohls.com/product/prd-1/x.jsp"),
                 pp.listing_kind("https://www.kohls.com/"))
                == ("category", "search", "product", "other"))
    ok &= check("category slug", pp.category_from_url(cat) == "womens-clothing")
    ok &= check("Kohl's own slug-less /catalog.jsp?CN=… is a category listing",
                pp.listing_kind("https://www.kohls.com/catalog.jsp?CN=Assortment:New%20Arrivals") == "category")
    enc = "https://www.kohls.com/search.jsp?submit-search=web-regular&search=c%2B%2B"
    ok &= check("page_url keeps the query's own encoding (c%2B%2B stays c%2B%2B)",
                pp.page_url(enc, 2) == enc + "&WS=120")
    ok &= check("host check", pp.is_supported_host(cat)
                and not pp.is_supported_host("https://www.kohls.co.uk/x"))
    return ok


def test_page_flow():
    group("page_flow: policy as data, and the served-page check")
    ok = True
    ok &= check("every state the classifier returns has a policy",
                set(page_flow.STATE_POLICY) == {"content", "empty", "end", "blocked",
                                                "challenge", "captcha", "unloaded"})
    ok &= check("a refusal is retried and counts as blocked",
                page_flow.should_retry("blocked") and page_flow.counts_as_blocked("blocked"))
    ok &= check("an empty page and the end of a listing are final answers",
                not any(page_flow.should_retry(s) for s in ("empty", "end", "content")))
    ok &= check("a blank page is retried but is NOT a block (exit 5, not 3)",
                page_flow.should_retry("unloaded") and not page_flow.counts_as_blocked("unloaded"))
    ok &= check("pages_to_fetch caps at the site's own total",
                page_flow.pages_to_fetch(50, 3) == 3 and page_flow.pages_to_fetch(2, 1317) == 2)
    ok &= check("...counting from the page the URL starts at",
                page_flow.pages_to_fetch(5, 7, start_page=6) == 2)
    ok &= check("...and plans what was asked when the total is unknown",
                page_flow.pages_to_fetch(4, None) == 4)
    p2, _ = fx("listing_featured_p2")
    base = "https://www.kohls.com/catalog/womens-clothing.jsp?CN=Gender:Womens+Department:Clothing"
    ok &= check("asked for 2, served 2: not another page",
                not page_flow.served_other_page(base + "&WS=120", p2))
    ok &= check("asked for 3, served 2: another page",
                page_flow.served_other_page(base + "&WS=240", p2))
    ok &= check("compared against the URL, not a loop counter (asked 1, served 2)",
                page_flow.served_other_page(base, p2))
    # wait_for_count: >= is success; a raising count is read as 0.
    calls = iter([0, 1])
    ok &= check("wait_for_count returns when the count REACHES the threshold",
                page_flow.wait_for_count(lambda s: next(calls), lambda ms: None, "x", 1, 5000) == 1)

    def boom(_):
        raise RuntimeError("navigated")
    ok &= check("a count that raises mid-navigation is polled, not fatal",
                page_flow.wait_for_count(boom, lambda ms: None, "x", 1, 0) == 0)
    # A policy constant nothing reads is dead code (§17): grep the consumer.
    tree = ast.parse(open(os.path.join(REPO_ROOT, "browser_bridge.py"), encoding="utf-8").read())
    loads = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    for const in ("BLOCK_RETRIES_WITHOUT_POOL", "MIN_CARD_MATCHES"):
        # A LOAD of the name, which an import line is not.
        ok &= check("page_flow.%s is read by the bridge, not just imported" % const,
                    const in loads)
    ok &= check("page_flow carries no policy nothing reads (no solve column)",
                all(set(v) == {"retry", "blocked"} for v in page_flow.STATE_POLICY.values())
                and not hasattr(page_flow, "should_solve"))
    return ok


# ---------------------------------------------------------------------------
# The whole run, with the browser stubbed out
# ---------------------------------------------------------------------------
class FakeDriver:
    """A driver that answers from fixtures. `plan` maps a page number (as the
    URL asks for it) to (status, html) or to an Exception to raise."""

    instances = []

    def __init__(self, args, pool, remote=False, plan=None):
        self.args, self.pool, self.remote = args, pool, remote
        self.plan = plan or {}
        self.url = ""
        self.gotos, self.relaunches = [], 0
        FakeDriver.instances.append(self)

    def open(self):
        return self

    def relaunch(self):
        self.relaunches += 1

    def close(self):
        pass

    def goto(self, url):
        self.gotos.append(url)
        self.url = url
        answer = self.plan.get(pp.page_number_from_url(url) if "prd-" not in url else "detail")
        if isinstance(answer, Exception):
            raise answer
        self._status, self._html = answer
        return self._status

    def content(self):
        return self._html

    def count(self, selector):
        if selector.startswith("astro-island"):
            return 1 if ("catalogData" in self._html or "ProductDetails" in self._html) else 0
        return len(re.findall(r"/product/prd-\d+", self._html))

    def current_url(self):
        return self.url

    def sleep(self, ms):
        pass

    def screenshot(self, path):
        pass

    def recaptcha_in_page(self):
        return None

    def inject_token(self, token):
        pass


def _run(argv, plan):
    FakeDriver.instances = []
    p = build_parser("test")
    with tempfile.TemporaryDirectory() as d:
        args = p.parse_args(argv + ["--out", os.path.join(d, "out"),
                                    "--delay", "0", "--retry-delay", "0"])
        validate_args(p, args)          # deliberately NOT env_config.apply
        rc = quiet(browser_bridge.scrape, args,
                   lambda a, pool, remote=False: FakeDriver(a, pool, remote, plan))
        meta_path = os.path.join(d, "out.meta.json")
        meta = json.load(open(meta_path)) if os.path.exists(meta_path) else None
        out_path = os.path.join(d, "out.json")
        rows = json.load(open(out_path)) if os.path.exists(out_path) else None
    return rc, meta, rows


def test_whole_run():
    group("scrape(): the public entry point, browser stubbed, fixtures served")
    ok = True
    s4, url4 = fx("listing_sort4_p1")
    f2, _ = fx("listing_featured_p2")
    end, _ = fx("end_page_not_available")
    deny, _ = fx("deny_dom_403")
    cha, _ = fx("challenge_sec_cpt")

    rc, meta, rows = _run(["--url", url4, "--pages", "2"], {1: (200, s4), 2: (200, f2)})
    ok &= check("2 pages served: exit 0, complete", rc == 0 and meta and meta["status"] == "complete")
    ok &= check("...18 rows, page+position unique",
                rows and len(rows) == 18 and len({(r["page"], r["position"]) for r in rows}) == 18)
    ok &= check("...the sidecar carries the site's own arithmetic",
                meta and meta["listing"].get("total_results") == 157955
                and meta["listing"].get("pages_available") == 1317)

    rc, meta, rows = _run(["--url", url4, "--pages", "3"], {1: (200, s4), 2: (200, f2), 3: (200, f2)})
    ok &= check("asked for page 3, served page 2: end_of_listing, still complete, exit 0",
                rc == 0 and meta["stop_reason"] == "end_of_listing"
                and meta["status"] == "complete" and meta["pages_completed"] == 2)

    cu, curl = fx("listing_cuisinart_p1")
    rc, meta, rows = _run(["--url", curl, "--pages", "50"], {1: (200, cu), 2: (404, end), 3: (404, end)})
    fetched = sum(len(d.gotos) for d in FakeDriver.instances)
    ok &= check("50 pages asked of a 3-page listing: at most 3 fetched (%d)" % fetched, fetched <= 3)
    ok &= check("...and it ends complete", rc == 0 and meta["status"] == "complete")

    rc, meta, rows = _run(["--url", url4], {1: (403, deny)})
    d = FakeDriver.instances[0]
    ok &= check("a refused page 1: exit 3, and no output or sidecar written",
                rc == EXIT_BLOCKED and meta is None and rows is None)
    ok &= check("...retried exactly BLOCK_RETRIES_WITHOUT_POOL times in a fresh session",
                len(d.gotos) == 1 + page_flow.BLOCK_RETRIES_WITHOUT_POOL
                and d.relaunches == page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    rc, _, _ = _run(["--url", url4], {1: (200, cha)})
    ok &= check("Akamai's challenge on page 1: exit 3", rc == EXIT_BLOCKED)
    rc, _, _ = _run(["--url", url4], {1: (200, "<html><head></head><body></body></html>")})
    ok &= check("a blank page 1: exit 5 (never obtained), not 3 and not 4",
                rc == EXIT_FETCH_FAILED)
    rc, _, _ = _run(["--url", url4], {1: DriverError("x", "ERR_PROXY_CONNECTION_FAILED")})
    ok &= check("a dead proxy: exit 5", rc == EXIT_FETCH_FAILED)
    rc, _, _ = _run(["--url", url4, "--retries", "2"], {1: DriverError("timed out")})
    ok &= check("a timeout on every attempt: exit 5", rc == EXIT_FETCH_FAILED)

    rc, meta, rows = _run(["--url", url4, "--pages", "2"], {1: (200, s4), 2: (403, deny)})
    ok &= check("page 2 refused after page 1 served: exit 6, partial, page 2 named",
                rc == EXIT_PARTIAL and meta["status"] == "partial" and meta["pages_failed"] == [2])

    base = "https://www.kohls.com/catalog/womens-clothing.jsp?CN=Gender:Womens+Department:Clothing"
    rc, meta, rows = _run(["--url", base + "&WS=120"], {2: (200, f2)})
    ok &= check("a run STARTED on WS=120 keeps its rows, stamped page 2",
                rc == 0 and rows and all(r["page"] == 2 for r in rows))

    tag = pp._ISLAND_RE.search(s4).group(0)
    props = json.loads(__import__("html").unescape(pp._ATTR_RE["props"].search(tag).group(1)))
    props["catalogData"][1]["products"] = [1, []]
    new_tag = pp._ATTR_RE["props"].sub(
        lambda m: 'props="%s"' % __import__("html").escape(json.dumps(props), quote=True), tag)
    empty = s4.replace(tag, new_tag)
    ok &= check("(the empty-listing fixture really has no products)",
                pp.catalog_data(empty) is not None and pp.catalog_data(empty)["products"] == [])
    rc, meta, rows = _run(["--url", url4], {1: (200, empty)})
    ok &= check("a served listing with no products: exit 4, an honest empty",
                rc == EXIT_NO_PRODUCTS)

    boom = RuntimeError("Target page, context or browser has been closed")
    try:
        rc, meta, rows = _run(["--url", url4, "--pages", "3"], {1: (200, s4), 2: (200, f2), 3: boom})
    except Exception:                      # the defect itself: the run propagated it
        rc, meta, rows = 1, None, None
    ok &= check("an UNEXPECTED exception on page 3 keeps pages 1-2: exit 6, partial",
                rc == EXIT_PARTIAL and rows and len(rows) == 18 and meta["pages_failed"] == [3])
    try:
        rc, _, _ = _run(["--url", url4], {1: boom})
    except Exception:
        rc = 1
    ok &= check("...and on page 1 it is exit 5, not a traceback", rc == EXIT_FETCH_FAILED)

    unparsed = pp._ISLAND_RE.sub("<div>", f2)
    unparsed = re.sub(r"\$\s?\d", "", unparsed)        # served, links present, no prices
    ok &= check("(the unparsed page is served content with no rows)",
                pp.detect_page_state(unparsed, status=200) == "content"
                and pp.parse_products(unparsed, url4) == [])
    rc, meta, rows = _run(["--url", url4, "--pages", "3"], {1: (200, s4), 2: (200, unparsed)})
    ok &= check("a SERVED page 2 that parses to nothing: partial (exit 6), named, not 'complete'",
                rc == EXIT_PARTIAL and meta["stop_reason"] == "served_but_unparsed")
    rc, meta, rows = _run(["--url", url4], {1: (200, unparsed)})
    ok &= check("...on page 1: exit 4 (the page was obtained), with its own reason in the log",
                rc == EXIT_NO_PRODUCTS)
    rc, meta, rows = _run(["--url", url4, "--pages", "2"], {1: (200, s4), 2: (None, end)})
    ok &= check("a past-the-end page with NO status (Selenium) and the asked URL: end_of_listing",
                rc == 0 and meta["stop_reason"] == "end_of_listing" and meta["pages_completed"] == 1
                and len(rows) == 10)
    p = build_parser("t")
    for bad in (["--retries", "0"], ["--proxy-block-retries", "-1"], ["--delay", "-1"]):
        ok &= check("%s is refused as bad usage" % " ".join(bad),
                    quiet(lambda: _raises_exit(lambda: validate_args(p, p.parse_args(["--url", url4] + bad)))))

    dt, durl = fx("detail_croft_tee")
    rc, meta, rows = _run(["--mode", "product", "--url", durl], {"detail": (200, dt)})
    ok &= check("--mode product: exit 0, one row with a brand",
                rc == 0 and rows and rows[0]["brand"] == "Croft & Barrow"
                and meta["stop_reason"] == "single_page_mode")
    return ok


def test_concurrent_dispatch():
    group("concurrent dispatch, browser stubbed out")
    ok = True

    class Args:
        delay = 0

    def run(specs, concurrency, rows_for, die_on=()):
        fetched, lock = [], threading.Lock()

        def fake_fetch(driver, args, pool, page_num, url):
            with lock:
                fetched.append(page_num)
            if page_num in die_on:
                raise RuntimeError("boom on %d" % page_num)
            o = PageOutcome(page_num=page_num, url=url, state="content")
            o.products = rows_for(page_num)
            if not o.products:
                o.state = "end"
            return o
        make = lambda a, pool, remote=False: FakeDriver(a, pool, remote)
        res, un, ex = quiet(fetch_pages_concurrently, Args(), None, specs,
                            concurrency, make, fetch=fake_fetch)
        return fetched, res, un, ex

    specs = [(n, "u%d" % n) for n in range(2, 12)]
    fetched, res, un, ex = run(specs, 4, lambda n: ["row"])
    ok &= check("every queued page fetched exactly once", sorted(fetched) == list(range(2, 12)))
    ok &= check("outcomes restorable to page order",
                [o.page_num for o in sorted(res, key=lambda o: o.page_num)] == list(range(2, 12)))
    specs = [(n, "u%d" % n) for n in range(2, 51)]
    fetched, res, un, ex = run(specs, 3, lambda n: [] if n >= 5 else ["row"])
    ok &= check("the end of the listing stops dispatch (%d fetched of 49)" % len(fetched),
                ex and len(fetched) <= 3 + 3)
    ok &= check("unattempted pages are reported, not counted as failed",
                un and all(o.ok for o in res))
    specs = [(n, "u%d" % n) for n in range(2, 8)]
    fetched, res, un, ex = run(specs, 3, lambda n: ["row"], die_on={3})
    by_page = {o.page_num: o for o in res}
    ok &= check("a worker whose page raises records THAT page as failed (not lost)",
                3 in by_page and not by_page[3].ok and by_page[3].error)
    ok &= check("...and every queued page is accounted for, as a result or unattempted",
                sorted(list(by_page) + un) == [n for n, _ in specs])
    ok &= check("...and the siblings' pages still come back ok",
                all(by_page[n].ok for n in by_page if n != 3))
    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"])
    ok &= check("three workers start on three different exits",
                len({browser_bridge.worker_pool(pool, i).current for i in range(3)}) == 3)
    return ok


# ---------------------------------------------------------------------------
# The output contract
# ---------------------------------------------------------------------------
FAMILY_PREFIX = ["source", "scraped_at", "url", "sku", "title", "brand", "price",
                 "currency", "original_price", "discount_pct", "rating",
                 "review_count", "in_stock", "image_url", "category", "price_source"]


def test_output_contract():
    group("the output contract")
    ok = True
    names = [f.name for f in fields(Product)]
    ok &= check("Product keeps the family prefix byte-identical and in order",
                names[:16] == FAMILY_PREFIX)
    ok &= check("exit codes", (EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_FETCH_FAILED, EXIT_PARTIAL)
                == (3, 4, 5, 6))
    ok &= check("end_of_listing is a COMPLETE stop reason", "end_of_listing" in COMPLETE_STOP_REASONS)
    ok &= check("currency has no guessed default", Product().currency is None)
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "o")
        ok &= check("zero rows write nothing (exit 4)",
                    quiet(save, [], out, "both") == EXIT_NO_PRODUCTS
                    and not os.path.exists(out + ".json"))
        write_csv([], out + ".csv")
        ok &= check("an empty CSV still carries its header",
                    open(out + ".csv").read().strip().split(",") == names)
        rc = quiet(finish_run, [], out, "both", False, blocked=False,
                   stop_reason="page_load_timeout", pages_requested=1,
                   pages_completed=0, start_url="u", final_url="u")
        ok &= check("nothing gathered and not complete: exit 5", rc == EXIT_FETCH_FAILED)
        rc = quiet(finish_run, [Product(sku="1", price=1.0)], out, "json", False,
                   blocked=False, stop_reason="end_of_listing", pages_requested=5,
                   pages_completed=3, start_url="u", final_url="u", pages_failed=[4])
        ok &= check("a complete reason with a failed page is still partial (exit 6)",
                    rc == EXIT_PARTIAL)
    seen = set()
    ok &= check("dedupe keeps the first and drops the rest",
                len(dedupe_by_key([Product(sku="1"), Product(sku="1"), Product(sku=None)], seen)) == 2)
    return ok


def test_diff():
    group("diff_runs")
    ok = True
    a = asdict(Product(sku="1", price=10.0, price_source="catalog", sort="Featured"))
    b = dict(a, price=9.0)
    ok &= check("a price move is a change", len(diff_products([a], [b])["changed"]) == 1)
    c = dict(a, price=9.0, price_source="dom")
    ok &= check("...unless the price_source differs too (source_changed)",
                len(diff_products([a], [c])["source_changed"]) == 1)
    d = dict(a, price_max=12.0)
    ok &= check("a moved range top is a change", len(diff_products([a], [d])["changed"]) == 1)
    import diff_runs
    with tempfile.TemporaryDirectory() as tmp:
        p1, p2 = os.path.join(tmp, "a.json"), os.path.join(tmp, "b.json")
        json.dump([a], open(p1, "w"))
        json.dump([dict(a, sort="Price Low-High")], open(p2, "w"))

        class Args:
            old, new = p1, p2
        ok &= check("runs under different orderings are refused",
                    quiet(diff_runs._check_comparable, Args()) is False)
    return ok


# ---------------------------------------------------------------------------
# Captcha, env, proxies, credentials
# ---------------------------------------------------------------------------
def test_captcha():
    group("captcha detection")
    ok = True
    html, url = fx("signin_recaptcha")
    c = detect_recaptcha_v3(html, url)
    ok &= check("the site's own reCAPTCHA v3 on /signin is detected",
                c is not None and c.kind == "recaptcha_v3" and c.action == "verify")
    for name in ("listing_sort4_p1", "detail_croft_tee", "search_crocs_p1"):
        ok &= check("%s: the static detector stays quiet despite the injected "
                    "recaptcha hunter scripts" % name, detect_recaptcha_v3(*fx(name)) is None)
    # §23: a cap nothing enforces is a bill. One call site per attempt.
    src = open(os.path.join(REPO_ROOT, "browser_bridge.py"), encoding="utf-8").read()
    calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "handle_captcha_if_present"]
    ok &= check("the solver is reached from exactly one call site (%d)" % len(calls),
                len(calls) == 1)
    return ok


def _reads_unset(raw):
    name = "KOHLS_CDP_ENDPOINT"
    saved = os.environ.get(name)
    try:
        os.environ[name] = raw
        with io.StringIO() as buf, redirect_stderr(buf):
            value = env_config.env_value(name)
    finally:
        if saved is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = saved
    return value is None


def test_env_config():
    group("env_config")
    ok = True
    ok &= check("the env keys are this site's",
                set(env_config.ENV_KEYS) == {"TWOCAPTCHA_KEY", "KOHLS_CDP_ENDPOINT",
                                             "KOHLS_PROXY", "KOHLS_URL"})
    documented = set()
    for line in open(os.path.join(REPO_ROOT, ".env.example"), encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            documented.add(line.split("=", 1)[0])
    ok &= check(".env.example documents exactly the variables the code reads",
                documented == set(env_config.ENV_KEYS))
    # A copied .env.example must read as UNSET, credentials included.
    for line in open(os.path.join(REPO_ROOT, ".env.example"), encoding="utf-8"):
        key, _, value = line.strip().partition("=")
        if key in ("TWOCAPTCHA_KEY", "KOHLS_CDP_ENDPOINT", "KOHLS_PROXY"):
            ok &= check("the .env.example value of %s reads as unset" % key, _reads_unset(value))
    ok &= check("a real value is not mistaken for a placeholder",
                _reads_unset("ws://login:supersecret@cb.2captcha.com:9222") is False)
    # The defect this repo's own first live setup hit: a REAL login left
    # inside braces was echoed to the terminal by the placeholder warning.
    login = "u" + "22d0" * 4
    buf = io.StringIO()
    import logging
    handler = logging.StreamHandler(buf)
    env_config.logger.addHandler(handler)
    try:
        _reads_unset("http://{%s}:{password}@eu.proxy.2captcha.com:2334" % login)
    finally:
        env_config.logger.removeHandler(handler)
    ok &= check("the placeholder warning never echoes a user's value", login not in buf.getvalue())
    ok &= check("...and still says what is wrong", "braces" in buf.getvalue())
    ok &= check("no variable is mapped onto --out", "out" not in env_config.ENV_KEYS.values())
    return ok


def test_proxy_and_masking():
    group("credentials never reach argv or logs")
    ok = True
    url = "http://user:secret@eu.proxy.2captcha.com:2334"
    ok &= check("mask() hides credentials and keeps host:port",
                "secret" not in mask(url) and "eu.proxy.2captcha.com:2334" in mask(url))
    pw = to_playwright(url)
    ok &= check("the browser's server string has no credentials", "secret" not in pw["server"])
    scrubbed, creds = split_credentials(url)
    ok &= check("split_credentials separates address and credentials",
                scrubbed == "http://eu.proxy.2captcha.com:2334" and creds == ("user", "secret"))
    many = "a ws://u:pass@h1:1 b ws://u:pass@h1:1 c http://u:pass@h2:2"
    ok &= check("masking is GLOBAL, every occurrence",
                "pass@" not in mask_credentials(many) and mask_credentials(many).count("***:***@") == 3)
    ok &= check("the proxy-error names are recognised",
                proxy_failure_in("net::ERR_TUNNEL_CONNECTION_FAILED at x") == "ERR_TUNNEL_CONNECTION_FAILED")
    import fingerprint_client as fpc
    import captcha_solver as cs
    example_key = "0123456789abcdef" * 2
    for name, mod in (("fingerprint_client", fpc), ("captcha_solver", cs)):
        ok &= check("%s redacts a key out of an error message" % name,
                    example_key not in mod._redact("GET https://api.2captcha.com/x?key=" + example_key))
    import scraper_api_client as sac
    pwd = "SeCr" + "EtPw"
    raw = "cdpurl=ws://acct:" + pwd + "@cb.2captcha.com:9222 key=" + example_key
    out = sac._redact_debug_header(raw)
    ok &= check("the Scraper API's x-debug header is redacted",
                pwd not in out and example_key not in out and "cb.2captcha.com:9222" in out)
    src = inspect.getsource(sac.fetch_html)
    ok &= check("...and so is its error BODY, which can echo the cdpurl",
                "_redact_debug_header(resp.text" in src)
    return ok


def test_fingerprint_helpers():
    group("fingerprint: one identity; the drivers apply the same command list")
    ok = True
    import fingerprint_client as fpc
    fp = {"userAgent": {"userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) X"},
          "intl": {"contentLocale": "en-US", "languages": ["en-US", "en"],
                   "timeZone": "America/New_York"},
          "screen": {"width": 1920, "height": 1080, "outerHeight": 1000,
                     "deviceScaleFactor": 1},
          "navigator": {"platform": "Win32"}}
    kw = fpc.playwright_context_kwargs(fp)
    cmds = dict(fpc.cdp_identity_commands(fp))
    ok &= check("the CDP form sets the same user agent",
                cmds["Network.setUserAgentOverride"]["userAgent"] == kw["user_agent"])
    ok &= check("...the same timezone and locale",
                cmds["Emulation.setTimezoneOverride"]["timezoneId"] == kw["timezone_id"]
                and cmds["Emulation.setLocaleOverride"]["locale"] == kw["locale"])
    ok &= check("...and installs the same init script (languages included)",
                "languages" in cmds["Page.addScriptToEvaluateOnNewDocument"]["source"])
    ok &= check("the UA is read from where the API puts it, not userAgent.value",
                fpc.fingerprint_user_agent({"userAgent": {"userAgent": "A"}}) == "A")
    for f in ("playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py"):
        src = open(os.path.join(REPO_ROOT, f), encoding="utf-8").read()
        ok &= check("%s never reads the non-existent userAgent.value" % f,
                    '.get("value")' not in src)
        ok &= check("%s sets no bare user-agent override of its own" % f,
                    "setUserAgent(" not in src and "_chrome_ua" not in src
                    and '"user_agent"' not in src)
    pp_src = open(os.path.join(REPO_ROOT, "puppeteer_scraper.py"), encoding="utf-8").read()
    # pyppeteer's CDPSession.send returns a Future, not a coroutine; handing
    # it to run_coroutine_threadsafe raised TypeError, so auto-solve was never
    # enabled and --fingerprint crashed. Every send must go through _send.
    ok &= check("pyppeteer sends CDP commands through the coroutine wrapper",
                "self._send(" in pp_src and "bridge.run(cdp.send(" not in pp_src
                and "bridge.run(client.send(" not in pp_src)
    for f in ("playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py"):
        src = open(os.path.join(REPO_ROOT, f), encoding="utf-8").read()
        body = src.split("def inject_token", 1)[1].split("\ndef ", 1)[0]
        ok &= check("%s does not reload after injecting a token (a reload discards it)" % f,
                    "reload" not in body.split("#")[0] and ".refresh()" not in body
                    and "self.page.reload(" not in body and "page.reload(" not in body)
    bridge_src = open(os.path.join(REPO_ROOT, "browser_bridge.py"), encoding="utf-8").read()
    ok &= check("--fp-tags defaults to ONE OS-family tag the API accepts",
                re.search(r'"--fp-tags", default="Windows"', bridge_src) is not None)
    return ok


# ---------------------------------------------------------------------------
# The engines
# ---------------------------------------------------------------------------
ENGINES = ("playwright_scraper", "puppeteer_scraper", "selenium_scraper")
DRIVER_OPS = {"open", "relaunch", "close", "goto", "content", "count",
              "current_url", "sleep", "screenshot", "recaptcha_in_page",
              "inject_token"}
DRIVER_LIBS = {"playwright_scraper": "playwright", "puppeteer_scraper": "pyppeteer",
               "selenium_scraper": "selenium"}


def test_engines(skips):
    group("engines: three drivers, one bridge")
    ok = True
    for name in ENGINES:
        tree = ast.parse(open(os.path.join(REPO_ROOT, name + ".py"), encoding="utf-8").read())
        top = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top.add(node.module.split(".")[0])
        ok &= check("%s imports %s at MODULE level (so an absent library skips)"
                    % (name, DRIVER_LIBS[name]), DRIVER_LIBS[name] in top)
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name.endswith("Driver")]
        methods = {m.name for c in classes for m in c.body if isinstance(m, ast.FunctionDef)}
        ok &= check("%s's driver implements every operation the bridge uses" % name,
                    DRIVER_OPS <= methods)
        src = open(os.path.join(REPO_ROOT, name + ".py"), encoding="utf-8").read()
        ok &= check("%s decides nothing itself (no loop, no exit codes)" % name,
                    "finish_run" not in src and "sys.exit(" not in src
                    and "browser_bridge.main(" in src)
        ok &= check("%s never waits on an evaluated string (CSP)" % name,
                    "wait_for_function" not in src and "waitForFunction" not in src)
    for name in ENGINES:
        try:
            importlib.import_module(name)
        except ImportError as e:
            skips.append("%s (%s)" % (name, e))
    # Flag parity: the CLI is one parser; only the documented extra differs.
    extras = {}
    for name in ENGINES:
        src = open(os.path.join(REPO_ROOT, name + ".py"), encoding="utf-8").read()
        # CLI flags only — `options.add_argument("--no-sandbox")` is a browser switch.
        extras[name] = set(re.findall(r'\bp\.add_argument\("(--[a-z-]+)"', src))
    ok &= check("only pyppeteer adds a flag of its own, and it is --chromium-path",
                extras == {"playwright_scraper": set(), "puppeteer_scraper": {"--chromium-path"},
                           "selenium_scraper": set()})
    flags = set(re.findall(r'"(--[a-z0-9-]+)"', inspect.getsource(build_parser)))
    contract = {"--url", "--pages", "--category", "--format", "--out", "--delay",
                "--retries", "--retry-delay", "--concurrency", "--proxy", "--proxy-file",
                "--proxy-rotate", "--proxy-shuffle", "--proxy-block-retries",
                "--twocaptcha-key", "--captcha-api", "--solve-captcha", "--min-score",
                "--cdp-endpoint", "--allow-empty", "--dump-html", "--headless",
                "--headful", "--fingerprint", "--fp-country", "--fp-tags", "--locale",
                "--mode"}
    ok &= check("the shared parser carries every family contract flag (missing: %s)"
                % sorted(contract - flags), contract <= flags)
    for flag in ("--anti" + "detect", "--country", "--marketplace", "--details"):
        ok &= check("no engine registers the removed flag %s" % flag, flag not in flags)
    return ok


# ---------------------------------------------------------------------------
# Repository hygiene
# ---------------------------------------------------------------------------
# Assembled from pieces, so this file can be scanned like every other one
# instead of exempting itself — the file most likely to acquire a stray
# phrase is the one a wholesale exemption never reads.
BANNED_PHRASES = (
    "cloud " + "browser", "anti" + "detect browser", "anti-" + "detect browser",
    "2scraper Anti" + "detect Browser", "gate." + "2p" + "rx.com", "2p" + "rx.com",
    "--anti" + "detect", "ANTI" + "DETECT_LOCAL_API",
)


def _shipped_text_files():
    out = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache")
                   and not os.path.exists(os.path.join(root, d, "pyvenv.cfg"))]
        for f in files:
            if f.endswith((".py", ".md", ".txt", ".toml", ".yml", ".yaml", ".example",
                           ".json", ".csv")) or f in ("Dockerfile", ".dockerignore", ".gitignore"):
                out.append(os.path.join(root, f))
    return out


def test_wording():
    group("wording")
    ok = True
    files = _shipped_text_files()
    ok &= check("the scan covers this file too", os.path.abspath(__file__) in
                [os.path.abspath(f) for f in files])
    for phrase in BANNED_PHRASES:
        offenders = [os.path.relpath(f, REPO_ROOT) for f in files
                     if phrase.lower() in open(f, encoding="utf-8", errors="replace").read().lower()]
        ok &= check("no shipped file says %r %s" % (phrase, offenders or ""), not offenders)
    # §19: never claim a captcha is beyond solving — only that this repo
    # does not implement a solver for it.
    claim = re.compile(r"\b(?:un" + r"solvable|can" + r"not\s+be\s+solved|can't\s+be\s+solved)\b", re.I)
    offenders = [os.path.relpath(f, REPO_ROOT) for f in files
                 if claim.search(open(f, encoding="utf-8", errors="replace").read())]
    ok &= check("no shipped file claims a captcha is beyond solving %s" % (offenders or ""),
                not offenders)
    readme = open(os.path.join(REPO_ROOT, "README.md"), encoding="utf-8").read()
    ok &= check("the README names the Scraping Browser API", "Scraping Browser API" in readme)
    ok &= check("the README names no competitor",
                not re.search(r"brightdata|oxylabs|smartproxy|zyte|scraperapi\.com", readme, re.I))
    return ok


def test_fixture_corpus_is_scrubbed():
    group("the committed fixtures carry no session material or personal data")
    ok = True
    corpus = "\n".join(v["html"] for v in FIXTURES.values())
    ok &= check("the corpus is not empty (%d KB)" % (len(corpus) // 1024), len(corpus) > 100000)
    patterns = {
        "a Koddi ad tracker": r"koddi\.io/event-collection",
        "a JWT": r"eyJ[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{10,}",
        "an email address": r"[\w.+-]+@[\w-]+\.(?:com|net|org)\b",
        "a 32-hex string": r"\b[0-9a-f]{32}\b",
        "a proxy credential": r"://[^\s/@\"]+:[^\s/@\"]+@",
        "an Akamai challenge-script path": r'src="/[A-Za-z0-9]{3,6}/[A-Za-z0-9]{3,6}/',
        "an unscrubbed request id": r'"requestId":\[0,"[0-9a-f-]{20,}',
    }
    for label, pattern in patterns.items():
        ok &= check("the fixtures contain no %s" % label, not re.search(pattern, corpus))
    ok &= check("the scrub marker is present (the scrub ran)", "SCRUBBED-by-make_fixtures" in corpus)
    return ok


_MODULE_DUNDERS = {"__file__", "__name__", "__doc__", "__package__", "__spec__",
                   "__loader__", "__builtins__", "__debug__"}


def _undefined_names(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    bound = set(dir(builtins)) | _MODULE_DUNDERS
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound |= {(a.asname or a.name.split(".")[0]) for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            bound |= {(a.asname or a.name) for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    return {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in bound}


def _unreachable(path):
    """Statements after return/raise/break/continue in the same block (§22)."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    found = []
    for node in ast.walk(tree):
        for attr in ("body", "orelse", "finalbody"):
            block = getattr(node, attr, None)
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block[:-1]):
                if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                    found.append(block[i + 1].lineno)
    return found


def test_static_analysis():
    group("names resolve; no dead code after a return")
    ok = True
    py = sorted(f for f in os.listdir(REPO_ROOT) if f.endswith(".py"))
    ok &= check("there are modules to scan (%d)" % len(py), len(py) >= 12)
    for f in py:
        path = os.path.join(REPO_ROOT, f)
        # The README claims Python 3.9. This machine may be newer; parsing
        # with feature_version=(3, 9) rejects 3.10+ syntax (match, X | Y in
        # annotations evaluated at runtime aside) before CI's 3.9 job has to.
        try:
            ast.parse(open(path, encoding="utf-8").read(), feature_version=(3, 9))
            parses = True
        except SyntaxError as e:
            parses = "line %s: %s" % (e.lineno, e.msg)
        ok &= check("%s parses as Python 3.9 %s" % (f, "" if parses is True else parses),
                    parses is True)
        missing = _undefined_names(path)
        ok &= check("%s references no undefined name %s" % (f, sorted(missing) or ""), not missing)
        dead = _unreachable(path)
        ok &= check("%s has no statement after a return/raise %s" % (f, dead or ""), not dead)
    return ok


def test_shared_calls_bind():
    group("every call into a shared module binds against its real signature")
    ok = True
    shared = {n: importlib.import_module(n) for n in
              ("product_parser", "output_writer", "page_flow", "proxy_pool",
               "captcha_solver", "env_config", "fingerprint_client", "browser_bridge")}
    callers = [e + ".py" for e in ENGINES] + ["browser_bridge.py", "page_flow.py",
                                             "scraper_api_client.py", "make_fixtures.py"]
    checked = 0
    for filename in callers:
        tree = ast.parse(open(os.path.join(REPO_ROOT, filename), encoding="utf-8").read())
        bound = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)
            elif isinstance(node, ast.arg):
                bound.add(node.arg)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
        direct, aliases = {}, {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in shared and not node.level:
                for a in node.names:
                    direct[a.asname or a.name] = (node.module, a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in shared:
                        aliases[a.asname or a.name] = a.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = None
            if isinstance(node.func, ast.Name) and node.func.id in direct:
                target = direct[node.func.id]
            elif (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                  and node.func.value.id in aliases and node.func.value.id not in bound):
                target = (aliases[node.func.value.id], node.func.attr)
            if target is None:
                continue
            module = shared[target[0]]
            if not hasattr(module, target[1]):
                ok &= check("%s:%d calls %s.%s, which does not exist"
                            % (filename, node.lineno, target[0], target[1]), False)
                continue
            callee = getattr(module, target[1])
            if not (inspect.isfunction(callee) or inspect.isclass(callee)):
                continue
            if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
                continue
            try:
                inspect.signature(callee).bind(*([None] * len(node.args)),
                                               **{k.arg: None for k in node.keywords})
                checked += 1
            except TypeError as exc:
                ok &= check("%s:%d %s.%s — %s" % (filename, node.lineno, target[0], target[1], exc), False)
            except ValueError:
                pass
    ok &= check("...and there were calls to bind (%d)" % checked, checked > 30)
    return ok


def test_dockerfile():
    group("the Docker image carries every module its entrypoint imports")
    ok = True
    raw = open(os.path.join(REPO_ROOT, "Dockerfile"), encoding="utf-8").read()
    joined = re.sub(r"\\\n\s*", " ", raw)
    copied = set()
    for line in joined.splitlines():
        if line.startswith("COPY "):
            copied.update(t for t in line.split() if t.endswith(".py"))
    m = re.search(r"ENTRYPOINT\s*\[([^\]]*)\]", joined)
    entry = next((x.strip().strip('"\'') for x in (m.group(1).split(",") if m else [])
                  if x.strip().strip('"\'').endswith(".py")), None)
    ok &= check("the Dockerfile names a Python entrypoint", bool(entry))
    if not entry:
        return ok
    local = {f[:-3] for f in os.listdir(REPO_ROOT) if f.endswith(".py")}

    def reached(mod, seen):
        if mod in seen:
            return seen
        seen.add(mod)
        for node in ast.walk(ast.parse(open(os.path.join(REPO_ROOT, mod + ".py"), encoding="utf-8").read())):
            names = ([a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import)
                     else [node.module.split(".")[0]] if isinstance(node, ast.ImportFrom) and node.module
                     else [])
            for n in names:
                if n in local:
                    reached(n, seen)
        return seen
    missing = sorted(m + ".py" for m in reached(entry[:-3], set()) if m + ".py" not in copied)
    ok &= check("every imported module is COPYed (missing: %s)" % (missing or "none"), not missing)
    ok &= check("the image copies no test suite and no fixtures",
                "smoke_test.py" not in copied and "fixtures_generated.json" not in joined)
    copy_lines = [l for l in joined.splitlines() if l.startswith(("COPY ", "ADD "))]
    ok &= check("no COPY/ADD line brings in a .env",
                not any(re.search(r"(^|\s)\.env(\s|$|\.)", l) for l in copy_lines))
    ignore = open(os.path.join(REPO_ROOT, ".dockerignore"), encoding="utf-8").read().split()
    ok &= check(".dockerignore excludes every .env variant", ".env*" in ignore)
    return ok


def test_sample_output():
    group("sample_output is cut from a real run")
    ok = True
    rows = json.load(open(os.path.join(REPO_ROOT, "sample_output.json"), encoding="utf-8"))
    names = [f.name for f in fields(Product)]
    ok &= check("the sample has rows", len(rows) > 0)
    ok &= check("its columns match Product exactly, in order",
                all(list(r) == names for r in rows))
    ok &= check("every row is a kohls.com product with a numeric webID",
                all(r["source"] == "kohls.com" and re.fullmatch(r"\d{3,}", r["sku"] or "")
                    and "/product/prd-%s/" % r["sku"] in r["url"] for r in rows))
    ok &= check("every row shows real provenance",
                all(r["price_source"] in ("catalog", "product", "dom", "jsonld") for r in rows))
    header = open(os.path.join(REPO_ROOT, "sample_output.csv"), encoding="utf-8").readline().strip()
    ok &= check("the sample CSV header matches the schema", header.split(",") == names)
    return ok


def test_ci_checks_wired():
    group("the shipped CI checks run, and pass on this repo")
    ok = True
    gh = os.path.join(REPO_ROOT, ".github")
    if not os.path.isdir(gh):
        # The Docker image deliberately carries no .github; the escape is the
        # WHOLE directory being absent, never one file inside it (§22).
        print("  (no .github directory here — skipping, as the image build does)")
        return ok
    script = os.path.join(gh, "ci_checks.py")
    ok &= check("ci_checks.py is present", os.path.exists(script))
    proc = subprocess.run([sys.executable, script, "--all"], cwd=REPO_ROOT,
                          capture_output=True, text=True)
    ok &= check("ci_checks.py --all passes on this repo (exit %d)" % proc.returncode,
                proc.returncode == 0)
    if proc.returncode:
        for line in (proc.stdout + proc.stderr).strip().splitlines()[-12:]:
            print("        " + line)
    wf = open(os.path.join(gh, "workflows", "tests.yml"), encoding="utf-8").read()
    ok &= check("tests.yml runs the shipped check rather than an inline copy",
                "ci_checks.py --all" in wf or "ci_checks.py --secret-check" in wf)
    return ok


def test_gitignore():
    group(".gitignore: what must be committed is, what must not be is not")
    ok = True
    if not os.path.isdir(os.path.join(REPO_ROOT, ".git")):
        print("  (not a git checkout — skipping)")
        return ok

    def ignored(path):
        return subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO_ROOT).returncode == 0
    # Both halves, because a rule that broad has to leave these alone or it
    # becomes a rule somebody switches off. The first line is not
    # hypothetical: `*.json` kept the fixtures out of the first commit's
    # dry run, which would have left CI with no fixtures at all.
    for path in ("fixtures_generated.json", "sample_output.json", "sample_output.csv",
                 "sample_output.meta.json", ".env.example", ".github/ci_checks.py",
                 "tests/test_smoke.py"):
        ok &= check("%s is committed, not ignored" % path, not ignored(path))
    for path in (".env", ".env.bak", ".env.local", "live/page.html", "out/run.json",
                 "runs/x_page1_debug.html", "dump.page3", "kohls_products.json",
                 "foo_page1_debug.png"):
        ok &= check("%s is ignored" % path, ignored(path))
    return ok


def test_scraper_api_payload():
    group("Scraper API: payload shape and status, with requests stubbed")
    ok = True
    import scraper_api_client as sac
    captured = {}

    class Resp:
        status_code = 200
        headers = {}
        text = ""

        def json(self):
            return {"status": "success", "http_code": 403, "body": "<html><body>x</body></html>"}

    real = sac.requests.post
    sac.requests.post = lambda url, **kw: (captured.update(kw.get("json") or {}), Resp())[1]

    class A:
        url = "https://www.kohls.com/catalog/cuisinart.jsp?CN=Brand:Cuisinart"
        timeout, cdp_url, key = 60, None, "k" * 8
        wait_text, wait_element, wait_state = "catalogData", None, None
    try:
        _html, status = quiet(sac.fetch_html, A())
    finally:
        sac.requests.post = real
    ok &= check("waitFor is an OBJECT, not a JSON string", captured.get("waitFor") == {"text": "catalogData"})
    ok &= check("the target status comes from http_code", status == 403)
    return ok


def main() -> int:
    import logging
    # The engines log every fetch at INFO; here that would bury the PASS/FAIL
    # lines. WARNING and above still print.
    logging.disable(logging.INFO)
    ok = True
    skips = []
    for t in (test_listing_values, test_sponsored_and_positions, test_jsonld_is_a_decoy,
              test_fallback_path, test_detail, test_page_state, test_urls, test_page_flow,
              test_whole_run, test_concurrent_dispatch, test_output_contract, test_diff,
              test_captcha, test_env_config, test_proxy_and_masking,
              test_fingerprint_helpers, test_wording, test_fixture_corpus_is_scrubbed,
              test_static_analysis, test_shared_calls_bind, test_dockerfile,
              test_sample_output, test_ci_checks_wired, test_gitignore, test_scraper_api_payload):
        ok &= t()
    ok &= test_engines(skips)
    print()
    if _failures:
        print("%d check(s) FAILED:" % len(_failures))
        for f in _failures:
            print("  - %s" % f)
    if skips:
        print("%d engine group(s) SKIPPED — an engine library is absent. CI's "
              "engine-smoke job fails if this list is non-empty for the engine "
              "it installed:" % len(skips))
        for s in skips:
            print("  - SKIPPED %s" % s)
    print("smoke_test: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
