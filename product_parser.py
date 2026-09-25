"""
product_parser.py
-----------------
Extracts rows from kohls.com category grids, search results and product
detail pages. This module is where essentially all of the site knowledge in
this repo lives; the engines carry about a dozen constants and nothing else.

Where the data comes from — measured, 2026-09-25
------------------------------------------------
kohls.com is an Astro site. Every listing page it serves (category grids
under /catalog/*.jsp and search results under /search.jsp) carries the whole
page of products SERVER-RENDERED into the props of one `<astro-island>`:

    <astro-island component-url="/shopnext-web-findability/_astro/index.….js"
                  props="{&quot;catalogData&quot;:[0,{…&quot;products&quot;:[1,[…]]…}]}">

`catalogData` holds the page's products (120 per page), and — which is what
makes this site pleasant to paginate — the site's own arithmetic about the
listing: `productCount`, `totalPages`, `limit`, and `currentPage`, the page
the SERVER actually answered with. That island is the primary source here.

JSON-LD is present and is NOT the primary source, and the reason is the
trap §24 of the family template describes. Every listing page carries exactly
one `application/ld+json` block, a `CollectionPage` whose `ItemList` holds
FIFTEEN items against the page's 120 — measured on eleven live listing
captures, all fifteen, every time, and always the first fifteen payload
entries INCLUDING sponsored ones. A JSON-LD-first parser would return 15 rows
per page and report success. The block is still read for exactly one thing:
it is the only place a listing page states its currency.

The fallback, used only when no island is found, anchors on the product URL
pattern (/product/prd-<digits>/) rather than on a class — Kohl's markup is
Tailwind utility classes that repeat across unrelated layout.

A detail page (/product/prd-NNN/…jsp) carries a different island
(`ProductDetails`, prop `product`) with the brand, per-SKU availability and
the price ranges, plus a JSON-LD `Product`. The island is primary there too:
the JSON-LD `offers` list held 50 SKUs against the island's 286 on the page
measured, all 50 of them in stock — it is a truncated, in-stock-only view.

Sponsored placements
--------------------
The listing payload interleaves sponsored entries (`sponsoredData` set, a
Koddi ad tracker inside) with the organic results: 0 to 12 per page across
the captures, and the count varies between two fetches of the same URL. They
are dropped, and `position` counts the rows this parser EMITS, so the same
organic result does not change position because an ad did.

Structured vs displayed price
-----------------------------
Checked, per the family rule: the island's current price was found among the
rendered tile's prices on 323 of 323 tiles across three pages. There is no
discount chain for a DOM overlay to reconcile, so there is none here —
porting one would be dead code that looks load-bearing.

Field coverage, measured on the 863 rows this parser emits from eight live
listing pages (2026-09-25)
--------------------------------------------------------------------------
    sku / title / url / price / currency / image     863/863
    original_price (a sale is running)               718/863
    price_max / original_price_max (a range)         128 / 25
    rating + review_count (non-zero count)           705/863
    in_stock                                         863/863, all True
    brand                                              0/863  <- see Product
"""

import html as html_lib
import json
import logging
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, urljoin

from bs4 import BeautifulSoup

from output_writer import Product, SOURCE_DEFAULT

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The site
# ---------------------------------------------------------------------------
# One shop. kohls.com publishes no hreflang alternates (checked on every
# capture), so there is no country table to derive. The currency here is a
# FALLBACK only, consulted when the DOM path read a bare "$": every
# structured row takes its currency from the page's own `priceCurrency`.
HOSTS: Dict[str, str] = {"kohls.com": "USD"}

# Hosts that are Kohl's and are still not supported, with the reason, so a
# caller is told what is actually wrong rather than "not a Kohl's site".
UNSUPPORTED: Dict[str, str] = {}


def site_host(url: str) -> str:
    """The bare hostname of `url` with a leading "www." removed; "" if unparseable."""
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def is_supported_host(url: str) -> bool:
    return site_host(url) in HOSTS


def unsupported_reason(url: str) -> Optional[str]:
    return UNSUPPORTED.get(site_host(url))


def host_currency(url: str) -> Optional[str]:
    return HOSTS.get(site_host(url))


SELECTORS = {
    # The fallback anchor, and the engines' "has the grid painted?" probe.
    # A Kohl's product URL is /product/prd-<digits>/<slug>.jsp. The probe
    # counts LINKS, and a tile carries about four of them (image, title,
    # swatches), so its thresholds are in links, not tiles.
    "item_link": 'a[href*="/product/prd-"]',
}

# The numeric id, and ONLY the numeric one. The same page links to "outfit"
# collections as /product/prd-c2435951/… — the same URL shape with a letter in
# front of the number, matched by the selector above and describing no single
# product. They are the "junk link" §4 of the family template warns about: in
# the fallback path they would become rows with a plausible title and a
# neighbour's price.
_SKU_IN_URL_RE = re.compile(r"/product/prd-(\d+)(?=[/.?#]|$)")


def sku_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = _SKU_IN_URL_RE.search(url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Astro's island props
# ---------------------------------------------------------------------------
# Astro serialises island props as nested `[type, value]` pairs: 0 a plain
# value (objects recursively), 1 an array (each element encoded again), and
# higher numbers for Date, Map, Set, BigInt, URL and typed arrays. The
# catalogue uses only 0 and 1; the others are decoded to their plain payload
# rather than raising, so an unexpected Date in a future deploy costs that
# field and not the page.
_ISLAND_RE = re.compile(r"<astro-island\b[^>]*>", re.IGNORECASE)
_ATTR_RE = {
    "component": re.compile(r'\bcomponent-url="([^"]*)"'),
    "props": re.compile(r'\bprops="([^"]*)"'),
}


def _astro_value(v):
    if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int):
        kind, value = v
        if kind == 0:
            if isinstance(value, dict):
                return {k: _astro_value(x) for k, x in value.items()}
            return value
        if kind == 1 and isinstance(value, list):
            return [_astro_value(x) for x in value]
        if kind == 4 and isinstance(value, list):  # Map: each entry is itself [1, [k, v]]
            out = {}
            for entry in value:
                pair = _astro_value(entry)
                if isinstance(pair, list) and len(pair) == 2:
                    out[str(pair[0])] = pair[1]
            return out
        if kind == 5 and isinstance(value, list):  # Set
            return [_astro_value(x) for x in value]
        if kind == 11:  # Infinity, signed by the value
            return float("inf") if value == 1 else float("-inf")
        return value
    if isinstance(v, dict):
        return {k: _astro_value(x) for k, x in v.items()}
    return v


def astro_islands(html: str) -> List[Tuple[str, dict]]:
    """Every island on the page as (component-url, decoded props).

    A regex over the opening tag rather than a soup: the props attribute of
    the catalogue island alone is ~1 MB, and building a DOM for the whole page
    just to read one attribute doubles the parse time for nothing. An island
    whose props do not decode is skipped — one malformed island must not
    cost the page.
    """
    out: List[Tuple[str, dict]] = []
    for tag in _ISLAND_RE.findall(html or ""):
        props = _ATTR_RE["props"].search(tag)
        if not props:
            continue
        comp = _ATTR_RE["component"].search(tag)
        try:
            raw = json.loads(html_lib.unescape(props.group(1)))
        except (ValueError, TypeError):
            logger.warning("Skipping an astro-island whose props did not parse.")
            continue
        if isinstance(raw, dict):
            out.append((comp.group(1) if comp else "",
                        {k: _astro_value(v) for k, v in raw.items()}))
    return out


def catalog_data(html: str) -> Optional[dict]:
    """The listing island's `catalogData`, or None when the page has none."""
    if not html or "catalogData" not in html:
        return None
    for _, props in astro_islands(html):
        data = props.get("catalogData")
        if isinstance(data, dict):
            return data
    return None


def _detail_product(html: str) -> Optional[dict]:
    """The detail page's `product` model, or None."""
    if not html or "ProductDetails" not in html:
        return None
    for comp, props in astro_islands(html):
        if "ProductDetails" in comp and isinstance(props.get("product"), dict):
            return props["product"]
    return None


# ---------------------------------------------------------------------------
# Page state
# ---------------------------------------------------------------------------
# Every marker below was COUNTED on pages known to be good before it was
# trusted, per the family rule — 14 served pages fetched through the
# Scraping Browser API (12 on 2026-09-25, 2 in August: listings, searches,
# two detail pages, /signin), the two past-the-end 404 fetches, against four
# real refusals (three deny pages and one behavioural challenge):
#
#     marker                         served  404  refused
#     Reference-id shape (below)          0    0      3/3 deny pages
#     errors.edgesuite.net                0    0      3/3
#     sec-if-cpt-container                0    0      1/1 challenge
#     cf-turnstile                       14    2      0     <- NOT a marker
#     captcha-widgets                    14    2      0     <- NOT a marker
#     akam (Akamai's sensor)             14    0      1     <- NOT a marker
#     designsystem.kohls.com             14    2      0     positive signal
#
# `cf-turnstile` and `<captcha-widgets>` are on every good page fetched over
# `--cdp-endpoint` because the Scraping Browser's auto-solve extension
# injects them (16 extension scripts per page); `akam` is Akamai's own
# sensor, which Kohl's loads on every page it serves. All three would have
# made a perfectly good page read as a challenge. None of them is in the set,
# so the set scores zero on the CDP capture WITHOUT any extension-tag strip —
# which is why this module has none.
#
# The deny page reaches the parser in two spellings. Raw bytes (curl,
# requests, the Scraper API) entity-escape the punctuation —
# `Reference&#32;&#35;18&#46;b0ec655f&#46;…` — while a browser's DOM
# serialises it back plainly. So the head of the page is unescaped before
# matching (bounded: a refusal is a few hundred bytes, a listing is 3 MB).
_UNESCAPE_PREFIX = 20000
_AKAMAI_REFERENCE_RE = re.compile(
    r"Reference\s*#\s*\d+\.[0-9a-f]+\.\d+\.[0-9a-f]+", re.IGNORECASE)

BOT_CHALLENGE_MARKERS = {
    # Akamai's refusal: "Access Denied" with a reference id and a link to
    # errors.edgesuite.net. Measured under HTTP 403 AND under HTTP 200 (real
    # Chrome, headful), so the status alone cannot be trusted to find it.
    "akamai-deny": ("errors.edgesuite.net",),
    # Akamai Bot Manager's behavioural challenge ("Powered and protected by
    # Akamai", a press-and-hold button). This repo does not implement a
    # solver for it; see README.
    "akamai-challenge": ("sec-if-cpt-container", "sec-bc-tile-container"),
    # The captcha loaders this site could plausibly render. Specific loader
    # PATHS, never a bare "recaptcha": the Scraping Browser injects
    # `…/captcha/recaptcha/hunter.js` into every page, which a bare word
    # would match.
    "recaptcha": ("www.google.com/recaptcha/api.js",
                  "www.google.com/recaptcha/enterprise.js",
                  "recaptcha/api2/anchor", "recaptcha/api2/bframe"),
    "hcaptcha": ("hcaptcha.com/1/api.js",),
    "turnstile": ("challenges.cloudflare.com",),
}

# A page Kohl's actually served is built out of its own design-system and
# media hosts; neither refusal references them once. This is what classifies
# a page that carries no marker at all — an empty body, Chromium's own
# network-error page — as not served, rather than as an empty listing.
SITE_ASSET_MARKERS = ("designsystem.kohls.com", "media.kohlsimg.com")

# A page number past the end of a listing: HTTP 404, the browser's URL
# becomes /catalog/page_not_available.jsp (measured on a one-page search
# asked for WS=120 and WS=240), and the page carries Kohl's own "not
# available" container. The CONTAINER is what decides, because neither of the
# other two signals always arrives: Selenium reports no status, and a driver
# or the Scraper API can report the URL that was ASKED for. The saved capture
# of that page has no `page_not_available` string in it at all — the URL is a
# property of the navigation, not of the document. 0 occurrences of the
# container on every served page.
PAGE_NOT_AVAILABLE = "page_not_available.jsp"
NOT_AVAILABLE_MARKERS = ('id="page_not_avail"', "pageNotAvailableContainer")


def _normalized_head(html: str) -> str:
    return html_lib.unescape(html[:_UNESCAPE_PREFIX]) + html[_UNESCAPE_PREFIX:]


def detect_bot_challenge(html: str, url: Optional[str] = None) -> Optional[str]:
    """Name what refused or challenged this page, or None."""
    if not html:
        return None
    text = _normalized_head(html)
    if _AKAMAI_REFERENCE_RE.search(text):
        return "akamai-deny"
    for vendor, markers in BOT_CHALLENGE_MARKERS.items():
        if any(m in text for m in markers):
            return vendor
    return None


_TAG_RE = re.compile(r"<[^>]*>")


def _is_blank(html: str) -> bool:
    """A document with no text and nothing in it: the page never arrived.

    Measured twice on 2026-09-25: Selenium through a proxy it could not
    authenticate returned `<html><head></head><body></body></html>` (39
    bytes) for the target URL instead of raising, and real Chrome answered a
    navigation with HTTP 200 and an empty document. Neither is a refusal —
    Akamai's refusal has a body and a reference id — so neither may be
    reported as one.
    """
    return len(html) < 2000 and not _TAG_RE.sub("", html).strip()


def detect_page_state(html: str, status: Optional[int] = None,
                      url: Optional[str] = None) -> str:
    """One of "content", "empty", "end", "blocked", "challenge", "captcha",
    "unloaded".

    Ordered by how much each signal PROVES, not by how cheap it is (§17):

      1. the site's structured data — content, or an honestly empty listing
         (only a served page carries the island or the product model);
      2. a refusal's own markers — only a refusal carries a reference id;
      3. the site's own "page not available" container (or URL) — the answer
         for a page past the end of a listing, served by Kohl's;
      4. the status, then the structural "built from Kohl's assets" test.

    A blank document is "unloaded" — the page never arrived — and is
    checked first, because every other rule would misread it.
    """
    html = html or ""
    if _is_blank(html):
        # Before everything else: nothing below can be decided about a
        # document that holds nothing.
        return "unloaded"
    # The site's own structured data first: a page carrying the listing
    # island or the product model was SERVED, whatever strings it also holds
    # — a marker may only refine the reason for a page already given up on
    # (§18), never overrule the data. No refusal capture carries either.
    data = catalog_data(html)
    if data is not None:
        products = data.get("products") or []
        return "content" if products else "empty"
    if _detail_product(html) is not None:
        return "content"
    vendor = detect_bot_challenge(html, url)
    if vendor == "akamai-deny":
        return "blocked"
    if vendor == "akamai-challenge":
        return "challenge"
    if (url and PAGE_NOT_AVAILABLE in url) or any(m in html for m in NOT_AVAILABLE_MARKERS):
        return "end"
    if vendor in ("recaptcha", "hcaptcha", "turnstile"):
        return "captcha"
    if status == 403:
        return "blocked"
    if not any(m in html for m in SITE_ASSET_MARKERS):
        # Not a refusal we can name, and not built out of Kohl's own assets:
        # an empty body, or the browser's own error page. It is not an
        # answer from the shop.
        return "blocked"
    if status == 404:
        return "end"
    if _SKU_IN_URL_RE.search(html):
        # Served, no island, product links present — the fallback path's case.
        return "content"
    # Served by Kohl's, and nothing on it — the prototype of this repo
    # reported that a bare /catalog/…jsp without its CN= filter redirects to
    # the homepage (not re-measured here). A correct answer either way.
    return "empty"


# ---------------------------------------------------------------------------
# URLs: page kind, pagination, category
# ---------------------------------------------------------------------------
def listing_kind(url: str) -> str:
    """"category", "search", "product" or "other"."""
    path = urlparse(url or "").path
    if "/product/prd-" in path:
        return "product"
    if path.startswith("/search.jsp"):
        return "search"
    # Both forms are Kohl's own: /catalog/womens-clothing.jsp?CN=… and the
    # slug-less /catalog.jsp?CN=… its navigation links to
    # (e.g. /catalog.jsp?CN=Assortment:New%20Arrivals).
    if path == "/catalog.jsp" or (path.startswith("/catalog/") and path.endswith(".jsp")):
        return "category"
    return "other"


# Kohl's pagination, measured 2026-09-25 by pressing the grid's own "Next
# Page" button and then fetching the URLs it produced as FRESH navigations:
#
#   the button rewrites the address to  …&WS=120&PPP=120
#   WS is a zero-based offset, PPP the page size.
#
#   &WS=120&PPP=120   -> currentPage 2, offset 121
#   &WS=120           -> currentPage 2, offset 121   (PPP not needed)
#   &WS=240&PPP=120   -> currentPage 3, offset 241
#   &WS=157800        -> currentPage 1316 of 1317
#   search.jsp&WS=120 -> currentPage 2 (search paginates the same way)
#   WS past the end   -> HTTP 404, redirected to page_not_available.jsp
#
# The pager is a <select> and a button with no href, so there is no
# next-link for anything to follow: construction is the only way, and the
# response's own `currentPage` is how every page is verified.
PAGE_PARAM = "WS"
PAGE_SIZE_PARAM = "PPP"
# The page size the site serves when the URL does not say (`limit` on every
# capture).
DEFAULT_PAGE_SIZE = 120


def _page_size(url: str) -> int:
    for k, v in parse_qsl(urlparse(url or "").query, keep_blank_values=True):
        if k == PAGE_SIZE_PARAM:
            try:
                n = int(v)
                if n > 0:
                    return n
            except ValueError:
                pass
    return DEFAULT_PAGE_SIZE


def page_url(url: str, page_num: int) -> str:
    """`url` addressing listing page `page_num` (1-based).

    Every existing parameter is preserved — the CN= filter is what makes a
    category URL a category at all — and WS is REPLACED rather than
    appended. The page size the URL already asks for is kept, so a caller's
    own PPP does not change which offset page N starts at.
    """
    parts = urlparse(url)
    size = _page_size(url)
    # The raw query is edited as TEXT, never decoded and re-encoded: a
    # round-trip turned `search=c%2B%2B` into `search=c++`, which the server
    # reads as spaces — a different search. Only the WS= segment is touched.
    kept = [seg for seg in parts.query.split("&")
            if seg and seg.split("=", 1)[0] != PAGE_PARAM]
    if page_num > 1:
        kept.append("%s=%d" % (PAGE_PARAM, (page_num - 1) * size))
    return urlunparse(parts._replace(query="&".join(kept)))


def page_number_from_url(url: str) -> Optional[int]:
    """The listing page `url` ASKS for; 1 when it carries no WS."""
    if not url:
        return None
    for k, v in parse_qsl(urlparse(url).query, keep_blank_values=True):
        if k == PAGE_PARAM:
            try:
                return int(v) // _page_size(url) + 1
            except ValueError:
                return None
    return 1


def category_from_url(url: str) -> Optional[str]:
    """A label out of a category URL: /catalog/womens-clothing.jsp -> "womens-clothing".

    None for a search or a product URL — a search term is the caller's own
    input, and belongs in --category if the caller wants it recorded.
    """
    if listing_kind(url) != "category" or urlparse(url).path == "/catalog.jsp":
        return None
    slug = urlparse(url).path.rsplit("/", 1)[-1]
    slug = re.sub(r"\.jsp$", "", slug)
    return slug or None


# ---------------------------------------------------------------------------
# The listing's own arithmetic
# ---------------------------------------------------------------------------
def listing_info(html: str) -> dict:
    """What the listing says about itself: totals, the page served, the sort."""
    data = catalog_data(html) or {}
    sort = next((s.get("name") for s in data.get("sorts") or []
                 if isinstance(s, dict) and s.get("active")), None)
    return {
        "total_results": _as_int(data.get("productCount")),
        "pages_available": _as_int(data.get("totalPages")),
        "per_page": _as_int(data.get("limit")),
        "served_page": _as_int(data.get("currentPage")),
        "sort": sort,
    }


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _as_int(v) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _clean_text(value) -> Optional[str]:
    if not value:
        return None
    return " ".join(str(value).split()) or None


def _discount_from(price: Optional[float], original: Optional[float]) -> Optional[float]:
    """Percentage off, computed rather than read. None unless original > price."""
    if original and price is not None and original > price:
        return round((1 - price / original) * 100, 1)
    return None


def _range(node) -> Tuple[Optional[float], Optional[float]]:
    """(min, max) of a {minPrice, maxPrice} node; max is None for one price."""
    if not isinstance(node, dict):
        return None, None
    lo, hi = _as_float(node.get("minPrice")), _as_float(node.get("maxPrice"))
    if hi is not None and lo is not None and hi <= lo:
        hi = None
    return lo, hi


def _rating(avg, count) -> Tuple[Optional[float], Optional[int]]:
    """(rating, review_count), both None when there are no reviews.

    The payload writes an unreviewed product as `avgRating: 0, count: 0`
    (158 of 863 rows). Zero is not a rating, and a count of zero with a
    null rating beside it would still tell an average-computing consumer
    something false, so the two go null together.
    """
    count = _as_int(count)
    value = _as_float(avg)
    if not count or value is None or not 0 < value <= 5:
        return None, None
    return round(value, 2), count


def _prices(price_node: dict) -> dict:
    """The price columns out of one {regularPrice, salePrice, …} node."""
    sale_lo, sale_hi = _range(price_node.get("salePrice"))
    reg_lo, reg_hi = _range(price_node.get("regularPrice"))
    if sale_lo is not None:
        price, price_max = sale_lo, sale_hi
        # The regular price is an ORIGINAL only when the sale is below it;
        # otherwise the two figures are not what they were taken for.
        original, original_max = ((reg_lo, reg_hi)
                                  if reg_lo is not None and reg_lo > sale_lo
                                  else (None, None))
    else:
        price, price_max = reg_lo, reg_hi
        original = original_max = None
    return {"price": price, "price_max": price_max,
            "original_price": original, "original_price_max": original_max,
            "discount_pct": _discount_from(price, original)}


# ---------------------------------------------------------------------------
# Currency, from the page's own JSON-LD
# ---------------------------------------------------------------------------
_LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                    re.IGNORECASE | re.DOTALL)


def _ld_blocks(html: str) -> List[dict]:
    out: List[dict] = []
    for raw in _LD_RE.findall(html or ""):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("Skipping an ld+json block that did not parse.")
            continue
        for block in data if isinstance(data, list) else [data]:
            if isinstance(block, dict):
                out.append(block)
    return out


def _offers(node) -> List[dict]:
    offers = node.get("offers") if isinstance(node, dict) else None
    if isinstance(offers, dict):
        return [offers]
    if isinstance(offers, list):
        return [o for o in offers if isinstance(o, dict)]
    return []


def page_currency(html: str) -> Optional[str]:
    """The currency the page's structured data states, or None.

    Only returned when every offer that names one names the SAME one — a
    page quoting two currencies has not told us which a given row is in.
    """
    found = set()
    for block in _ld_blocks(html):
        nodes = [block]
        entity = block.get("mainEntity")
        if isinstance(entity, dict):
            nodes += [(e.get("item") if isinstance(e, dict) else None)
                      for e in entity.get("itemListElement") or []]
        for node in nodes:
            for offer in _offers(node):
                cur = offer.get("priceCurrency")
                if isinstance(cur, str) and re.fullmatch(r"[A-Z]{3}", cur):
                    found.add(cur)
    return found.pop() if len(found) == 1 else None


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
def _absolute(base_url: str, href: Optional[str]) -> str:
    return urljoin(base_url, href) if href else ""


def _in_stock_listing(item: dict) -> Optional[bool]:
    ship = item.get("isAvailableforShip")
    pickup = item.get("isAvailableforPickUp")
    if ship is None and pickup is None:
        return None
    return bool(ship) or bool(pickup)


# The payload's own `prodType`, as an ALLOWLIST. Organic results are
# "product"; the grid also carries "collection" entries — an outfit such as
# "Women's Croft & Barrow® Spring Outfit" (webID c2435951, a URL of the same
# /product/prd-… shape) whose price is the RANGE of its member products,
# 6.99-176.00 on one live row. Not a product, and in a price column a
# misleading one. They first appeared in a live run on 2026-09-25 (2 of 122
# entries on one page) and were absent from every earlier capture, which is
# the argument for an allowlist: a kind nobody has seen yet is logged by name
# and dropped, rather than silently priced as if it were a product.
_ROW_KINDS = {"product"}


def parse_products(html: str, base_url: str, category: Optional[str] = None
                   ) -> List[Product]:
    """Rows from a listing page: the catalogue island first, URL fallback second."""
    label = category or category_from_url(base_url)
    data = catalog_data(html)
    if data is not None:
        return _parse_catalog(data, html, base_url, label)
    return _parse_url_fallback(html, base_url, label)


def _parse_catalog(data: dict, html: str, base_url: str,
                   label: Optional[str]) -> List[Product]:
    info = listing_info(html)
    # The page number the SERVER reports, never the one we think we asked
    # for: a page past the end, or a URL that already carried WS=, would
    # otherwise stamp rows with the wrong page.
    page = info["served_page"] or page_number_from_url(base_url)
    currency = page_currency(html)
    rows: List[Product] = []
    sponsored = 0
    other_kinds: Dict[str, int] = {}
    for item in data.get("products") or []:
        if not isinstance(item, dict):
            continue
        if item.get("sponsoredData"):
            sponsored += 1
            continue
        kind = item.get("prodType")
        if kind not in _ROW_KINDS:
            other_kinds[str(kind)] = other_kinds.get(str(kind), 0) + 1
            continue
        sku = _clean_text(item.get("webID"))
        url = _absolute(base_url, item.get("seoURL"))
        if not sku:
            sku = sku_from_url(url)
        if not sku or not url:
            continue
        price_nodes = [p for p in item.get("prices") or [] if isinstance(p, dict)]
        # One node per product on every capture; if the site ever sends
        # several, the one it marks current is the one being charged.
        node = next((p for p in price_nodes if p.get("isCurrentPrice")),
                    price_nodes[0] if price_nodes else {})
        cols = _prices(node)
        rating = item.get("rating") if isinstance(item.get("rating"), dict) else {}
        stars, count = _rating(rating.get("avgRating"), rating.get("count"))
        image = item.get("image") if isinstance(item.get("image"), dict) else {}
        rows.append(Product(
            source=SOURCE_DEFAULT, url=url, sku=sku,
            title=_clean_text(item.get("productTitle")),
            currency=currency if cols["price"] is not None else None,
            rating=stars, review_count=count,
            in_stock=_in_stock_listing(item),
            image_url=image.get("url") or None,
            category=label, price_source="catalog",
            page=page, position=len(rows) + 1,
            price_label=_clean_text(node.get("priceLabel")),
            sort=info["sort"],
            **cols))
    if sponsored:
        logger.info("Dropped %d sponsored placement(s) from page %s; positions "
                    "count organic rows only.", sponsored, page)
    if other_kinds:
        logger.info("Dropped %s from page %s: not single products.",
                    ", ".join("%d %r entr%s" % (n, k, "y" if n == 1 else "ies")
                              for k, n in sorted(other_kinds.items())), page)
    return rows


# --- URL-pattern fallback ---------------------------------------------------
_MAX_TILE_WIDEN = 8
_AMOUNT = r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)"
_PRICE_RUN_RE = re.compile(_AMOUNT + r"(?:\s*-\s*" + _AMOUNT + r")?")
_STARS_RE = re.compile(r"([\d.]+)\s*out of 5 stars", re.IGNORECASE)
_COUNT_RE = re.compile(r"\(\s*([\d,]+)\s*\)")
_REGULAR_LABELS = ("Reg", "Orig")


def _money(s: Optional[str]) -> Optional[float]:
    return float(s.replace(",", "")) if s else None


def _ids_in(node) -> set:
    return {m.group(1) for a in node.select(SELECTORS["item_link"])
            for m in [_SKU_IN_URL_RE.search(a.get("href") or "")] if m}


def _tile_scope(anchor):
    """The outermost ancestor still covering exactly ONE product id.

    Distinct ids, not links: a Kohl's tile links to its product about four
    times (image, title, swatches), so "stop at a second link" never leaves
    the anchor. Capped so a malformed page cannot walk to <body>.
    """
    best, node = anchor, anchor
    for _ in range(_MAX_TILE_WIDEN):
        node = node.parent
        if node is None or node.name in ("body", "html") or len(_ids_in(node)) > 1:
            break
        best = node
    return best


def _parse_url_fallback(html: str, base_url: str, label: Optional[str]) -> List[Product]:
    """Rows from rendered tiles, for a page with no catalogue island.

    Weaker by construction, and says so in `price_source`: prices are read
    out of the tile's TEXT — "$17.99 - $23.99 $29.99 Reg." — where the first
    amount (or range) is the current price and the amount followed by a
    regular-price label is the regular one. Kohl's writes that label two
    ways: "Reg." on most tiles and "Orig." on some ("$37.49 Sale $49.99
    Orig.", Crocs); reading only the first lost the original price on 3 of 85
    rows of one search.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    # The page's JSON-LD first. Failing that, the host's currency — and that
    # is the "$" the tile itself prints (every amount this path reads is
    # matched with its "$"), read as USD on a US-only shop: §4's bare-symbol
    # tier, a guess the site's own symbol supports, not a compiled default.
    currency = page_currency(html) or host_currency(base_url)
    page = page_number_from_url(base_url)
    # Only tiles inside the GRID. A listing page also renders a 24-product
    # recommendation carousel below it, whose tiles have the same URL shape,
    # prices and ratings — read without this, 9 to 24 products that are not
    # in the listing came back as rows. The grid is the parent holding the
    # most tiles: 60 to 128 on every capture measured, every one of them in
    # the page's own catalogue, against the carousel's 24.
    tiles = []
    for anchor in soup.select(SELECTORS["item_link"]):
        sku = sku_from_url(anchor.get("href"))
        if sku:
            tiles.append((anchor, sku, _tile_scope(anchor)))
    by_parent: Dict[int, int] = {}
    for _, _, tile in tiles:
        by_parent[id(tile.parent)] = by_parent.get(id(tile.parent), 0) + 1
    grid = max(by_parent, key=by_parent.get) if by_parent else None
    rows: List[Product] = []
    seen = set()
    for anchor, sku, tile in tiles:
        if sku in seen or id(tile.parent) != grid:
            continue
        text = " ".join(tile.get_text(" ").split())
        if re.search(r"\bSponsored\b", text):
            seen.add(sku)
            continue
        runs = list(_PRICE_RUN_RE.finditer(text))
        if not runs:
            # No price in scope: a link outside the grid (a recommendation
            # strip, a promo). Dropped rather than emitted as a priceless row.
            continue
        seen.add(sku)
        current = runs[0]
        price, price_max = _money(current.group(1)), _money(current.group(2))
        original = original_max = None
        for run in runs[1:]:
            if text[run.end():run.end() + 7].strip().startswith(_REGULAR_LABELS):
                original, original_max = _money(run.group(1)), _money(run.group(2))
                break
        else:
            # Some tiles print the regular price with NO label at all
            # ("$1,139.99 $1,199.99"; 9 of 736 fallback rows on seven pages).
            # The second amount is taken as the regular price only when it is
            # ABOVE the current one — the check below drops it otherwise —
            # which matched the catalogue on every such tile measured.
            if len(runs) > 1:
                original, original_max = _money(runs[1].group(1)), _money(runs[1].group(2))
        if original is not None and price is not None and original <= price:
            original = original_max = None
        img = tile.find("img")
        stars = _STARS_RE.search(" ".join(
            [text] + [el.get("aria-label", "") for el in tile.select("[aria-label]")]))
        count = _COUNT_RE.search(text)
        rating, reviews = _rating(stars.group(1) if stars else None,
                                  count.group(1).replace(",", "") if count else None)
        rows.append(Product(
            source=SOURCE_DEFAULT, url=_absolute(base_url, anchor.get("href")).split("?")[0],
            sku=sku, title=_clean_text(img.get("alt") if img else anchor.get_text()),
            price=price, price_max=price_max, currency=currency if price is not None else None,
            original_price=original, original_price_max=original_max,
            discount_pct=_discount_from(price, original),
            rating=rating, review_count=reviews,
            image_url=(img.get("src") if img else None) or None,
            category=label, price_source="dom", page=page, position=len(rows) + 1))
    return rows


# ---------------------------------------------------------------------------
# Detail page
# ---------------------------------------------------------------------------
# Per-SKU availability, as an ALLOWLIST: an unanticipated value reads as not
# available rather than as available.
_IN_STOCK_VALUES = {"In Stock"}


def parse_product_detail(html: str, base_url: str, category: Optional[str] = None
                         ) -> Optional[Product]:
    """One row from a /product/prd-NNN/ page, or None."""
    prod = _detail_product(html)
    currency = page_currency(html)
    if prod is None:
        return _detail_from_ld(html, base_url, category, currency)
    sku = _clean_text(prod.get("webID")) or sku_from_url(base_url)
    price_node = prod.get("price") if isinstance(prod.get("price"), dict) else {}
    cols = _prices(price_node)
    skus = [s for s in prod.get("SKUS") or [] if isinstance(s, dict)]
    in_stock_n = sum(1 for s in skus if s.get("availability") in _IN_STOCK_VALUES)
    stars, count = _rating(prod.get("avgRating"), prod.get("ratingCount"))
    images = [i for i in prod.get("images") or [] if isinstance(i, dict)]
    # The deepest breadcrumb that is a CATEGORY. Kohl's ends the trail with
    # the brand ("Womens > Clothing > Tops > Croft & Barrow", the last one
    # carrying `currentDimensionId: "Brand:…"`), which would put the brand
    # in the category column.
    crumbs = [c for c in prod.get("breadcrumbs") or [] if isinstance(c, dict)
              and not str(c.get("currentDimensionId") or "").startswith("Brand:")]
    url = _absolute(base_url, prod.get("seoURL")) or base_url.split("?")[0]
    return Product(
        source=SOURCE_DEFAULT, url=url, sku=sku,
        title=_clean_text(prod.get("productTitle")),
        brand=_clean_text(prod.get("brand")),
        currency=currency if cols["price"] is not None else None,
        rating=stars, review_count=count,
        in_stock=(in_stock_n > 0) if skus else None,
        image_url=(images[0].get("url") if images else None) or None,
        category=category or (_clean_text(crumbs[-1].get("name")) if crumbs else None),
        price_source="product",
        skus_total=len(skus) if skus else None,
        skus_in_stock=in_stock_n if skus else None,
        **cols)


def _detail_from_ld(html: str, base_url: str, category: Optional[str],
                    currency: Optional[str]) -> Optional[Product]:
    """A thinner row from the detail page's JSON-LD `Product`, when the island
    is missing. No original price and no stock counts: the JSON-LD carries
    neither (its offers are a truncated, in-stock-only subset)."""
    node = next((b for b in _ld_blocks(html) if b.get("@type") == "Product"), None)
    if node is None:
        return None
    prices = [p for p in (_as_float(o.get("price")) for o in _offers(node)) if p is not None]
    brand = node.get("brand")
    brand = brand.get("name") if isinstance(brand, dict) else brand
    rating = node.get("aggregateRating") if isinstance(node.get("aggregateRating"), dict) else {}
    stars, count = _rating(rating.get("ratingValue"),
                           rating.get("ratingCount") or rating.get("reviewCount"))
    image = node.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        image = image.get("url") or image.get("contentUrl")
    lo = min(prices) if prices else None
    hi = max(prices) if prices and max(prices) > min(prices) else None
    return Product(
        source=SOURCE_DEFAULT, url=node.get("url") or base_url.split("?")[0],
        sku=sku_from_url(node.get("url") or base_url),
        title=_clean_text(node.get("name")), brand=_clean_text(brand),
        price=lo, price_max=hi, currency=currency if lo is not None else None,
        rating=stars, review_count=count,
        image_url=image if isinstance(image, str) else None,
        category=category, price_source="jsonld")
