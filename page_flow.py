"""
page_flow.py
------------
kohls.com's page-state policy, shared by all three engines.

Why this module exists: kohls.com answers a request in six ways, and they
want different responses. Three copies of that triage would drift, and the
drift would be silent — one engine reporting exit 3 where its twin reports
exit 0 on the same page. The family already shares
`output_writer.finish_run()` for exactly this reason; this is the same
argument applied to the decisions that come before it.

    content    the listing island (or a detail page's product model) is there
    empty      a page Kohl's served with nothing on it (a search with no
               results; the prototype reported a CN-less category URL
               redirecting to the homepage — not re-measured)
    end        a page number past the end of the listing: HTTP 404 and a
               redirect to Kohl's own /catalog/page_not_available.jsp
    blocked    Akamai's "Access Denied" (under 403, and measured under 200)
    challenge  Akamai Bot Manager's behavioural challenge (sec-cpt)
    captcha    a captcha widget the site rendered
    unloaded   a blank document: the page never arrived (a load failure)

Measured 2026-09-25; every number is in product_parser.py beside the rule it
supports.

What this module deliberately does NOT contain
----------------------------------------------
No scrolling and no lazy-load hydration. The whole page of products is
SERVER-rendered into the listing island's props, 120 of them, before any
script runs — the island is in the first response, so a run that waited for
tiles to paint would be waiting for a copy of data it already has. And no
next-link following: Kohl's pager is a <select> and a button with no href,
so there is nothing to follow. Pages are addressed by `WS=` offset and each
one is VERIFIED by the page number the server says it served.

The functions here are pure or take the driver's primitives, so each engine
keeps its browser plumbing to itself, and no JavaScript crosses the boundary:

    count(selector) -> int                how many elements match
    sleep(ms) -> None                     the driver's own wait
"""

import logging
import time
from typing import Callable, Optional

from product_parser import (detect_page_state, page_url, page_number_from_url,
                            listing_info)

logger = logging.getLogger("page_flow")


# What "the page has arrived" means, per mode — the ISLAND carrying the data,
# not a count of painted tiles. Found in the first response on every capture,
# which is what makes a threshold of one safe here: the family's "must be >1"
# rule exists because a GENERIC selector (a product link) resolves on an
# unrelated element long before the grid paints, and an island whose props
# contain `catalogData` cannot be an unrelated element.
READY_SELECTOR_LISTING = 'astro-island[props*="catalogData"]'
READY_SELECTOR_PRODUCT = 'astro-island[component-url*="ProductDetails"]'

# How many product LINKS mean "the products are already on the page", for the
# captcha rule "detected is not blocking": a challenge on a page whose
# products are readable guards nothing. A tile carries about four links (506
# links for 128 payload entries on one capture), so 20 is five tiles — above
# anything a header or a promo strip holds, and well below the smallest
# listing captured (60 products, 414 product links).
MIN_CARD_MATCHES = 20

CONTENT_TIMEOUT_MS = {"listing": 20000, "product": 20000}
POLL_MS = 250


def ready_selector(mode: str) -> str:
    return READY_SELECTOR_PRODUCT if mode == "product" else READY_SELECTOR_LISTING


def min_matches(mode: str) -> int:
    """How many readiness anchors mean "arrived": one island, either mode."""
    return 1


def content_timeout_ms(mode: str) -> int:
    return CONTENT_TIMEOUT_MS.get(mode, 20000)


def wait_for_count(count: Callable[[str], int], sleep: Callable[[int], None],
                   selector: str, threshold: int, timeout_ms: int) -> int:
    """Poll `count(selector)` until it reaches `threshold`; return the last count.

    Polled through the driver's querySelectorAll — a protocol call — rather
    than `wait_for_function("…")`, which hands the browser a STRING to eval
    and dies under any Content-Security-Policy without 'unsafe-eval' (a
    sibling repo crashed with exit 1 on its most obvious URL that way).

    `>=`, not `>`: reaching the threshold IS the success case. `>` reported a
    fully-arrived page as not arrived whenever the page held exactly the
    threshold — invisible until a query had fewer results than the floor.
    A count that raises (the page navigated under it) is read as 0 and
    polled again.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    found = 0
    while True:
        try:
            found = count(selector)
        except Exception:  # noqa: BLE001 — a navigation mid-poll is not fatal
            found = 0
        if found >= threshold or time.monotonic() >= deadline:
            return found
        sleep(POLL_MS)


def classify(html: Optional[str], status: Optional[int] = None,
             url: Optional[str] = None) -> str:
    """The page's state, as the engines see it. One name, three callers."""
    return detect_page_state(html or "", status=status, url=url)


# What each state means for the run, as DATA, so an engine cannot quietly
# disagree with its twins about whether a page is worth retrying or paying for.
#
#   retry     fetch it again, from another exit when there is one
#   blocked   count it towards the blocked tally that decides exit 3
#
# There is no "solve" column: the captcha check runs on every fetch before
# the state is decided (a solve can change it), and a column no engine
# consulted would be policy that reads like enforcement and is not (§17).
STATE_POLICY = {
    "content": {"retry": False, "blocked": False},
    # A correct answer. Not retried: retrying spends the user's budget on
    # getting the same right answer again.
    "empty": {"retry": False, "blocked": False},
    # The listing ended. Not retried and not blocked; the engines record it
    # as `end_of_listing`, a COMPLETE stop reason.
    "end": {"retry": False, "blocked": False},
    # Akamai's refusal carries no challenge and no widget, so no solve is
    # attempted or billed. A different client or exit is what changed it.
    "blocked": {"retry": True, "blocked": True},
    # Akamai's behavioural challenge. This repo does NOT implement a solver
    # for it — which is a TODO, not a claim about what can be solved. Over
    # --cdp-endpoint the Scraping Browser's own Captcha.setAutoSolve gets the
    # first turn; locally a different exit is the fallback.
    "challenge": {"retry": True, "blocked": True},
    "captcha": {"retry": True, "blocked": True},
    # The document came back blank (see product_parser._is_blank). Retried,
    # and if it stays blank the page is a LOAD FAILURE — exit 5, "the content
    # was never obtained" — not a refusal: calling it blocked sent Selenium to
    # exit 3 where its twins reported exit 5 for the very same dead proxy.
    "unloaded": {"retry": True, "blocked": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, {}).get("retry", False)


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, {}).get("blocked", False)


# Every blocked or challenged page may be retried at most this many times
# when there is no pool to rotate through. One, not three: on 2026-09-25 no
# retry from the same exit and client ever turned Akamai's refusal into a
# page, so a second identical attempt confirms the answer rather than
# changing it — and one fresh session is cheap insurance against a scored
# cookie jar.
BLOCK_RETRIES_WITHOUT_POOL = 1


def served_other_page(asked_url: str, html: str) -> bool:
    """True when the server answered with a DIFFERENT page than `asked_url` asked for.

    Compared against the page the URL ASKS for (its WS=), never against the
    run's loop counter: a run started on a URL that already carries WS=240
    calls it page 1 of the run while asking the site for page 3, and a
    counter-based check would read "asked 1, got 3" and throw a good page
    away (a sibling repo lost a whole run to exactly that).

    Only a page that STATES which page it is can be judged; one that states
    nothing (a detail page, the fallback path) is not second-guessed.
    """
    served = listing_info(html).get("served_page")
    asked = page_number_from_url(asked_url)
    return served is not None and asked is not None and served != asked


def pages_to_fetch(pages_requested: int, pages_available: Optional[int],
                   start_page: int = 1) -> int:
    """How many pages this run should fetch, planned against the site's own total.

    Kohl's states `totalPages` on page 1, and a page past it is a 404 — so a
    run asked for 50 pages of a 3-page brand listing stops at 3 without
    spending fetches finding the end. `start_page` is the page the run's URL
    asks for, which need not be 1. Unknown total: plan what was asked, and
    let `served_other_page` and the 404 end it.
    """
    if not pages_available:
        return pages_requested
    return max(1, min(pages_requested, pages_available - start_page + 1))
