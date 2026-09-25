"""
browser_bridge.py
-----------------
Everything the three browser engines share, so they cannot drift apart.

The engines differ only in how they start a browser and how they ask it for
a page. Everything that decides what a run DOES — retries, exit rotation,
the page-state policy, page planning against the site's own total, the
served-page check, concurrency, the exit code — lives here, once. The family
rule is that all three engines "must agree on exit codes, run status, and
whether a run crashes or spends money"; three copies of a 600-line loop is
how they stop agreeing, silently.

Deliberately NO JavaScript crosses this boundary. Selenium's `execute_script`
takes a function BODY with an explicit `return`, while Playwright and
pyppeteer take `() => expr`, so a shared module that passed JS would quietly
acquire one driver's dialect. The driver protocol names OPERATIONS instead:

    open() -> driver            start a browser (or attach over CDP)
    relaunch()                  a fresh session: new browser locally, new page remotely
    close()
    goto(url) -> Optional[int]  navigate; the HTTP status where the driver has one.
                                Raises DriverError (proxy_failure set for a dead exit)
    content() -> str            the current document, "" if it will not hold still
    count(selector) -> int      querySelectorAll(selector).length
    current_url() -> str
    sleep(ms)
    screenshot(path)
    recaptcha_in_page() -> Optional[CaptchaChallenge]   the runtime detector
    inject_token(token)         and reload

Each engine module defines its driver and calls `main()`.
"""

import argparse
import logging
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from captcha_solver import (detect_recaptcha_v3, reconcile_detections,
                            solve_recaptcha)
from product_parser import (parse_products, parse_product_detail, SELECTORS,
                            detect_bot_challenge, page_url, page_number_from_url,
                            listing_kind, listing_info, site_host,
                            is_supported_host, HOSTS, unsupported_reason)
from output_writer import dedupe_by_key, finish_run, EXIT_FETCH_FAILED
import page_flow
from page_flow import MIN_CARD_MATCHES, BLOCK_RETRIES_WITHOUT_POOL
from proxy_pool import (from_args as proxy_pool_from_args, mask, ROTATE_MODES,
                        ProxyError, ProxyPool)
import env_config
from fingerprint_client import FingerprintError

logger = logging.getLogger("kohls")

ITEM_LINK_SELECTOR = SELECTORS["item_link"]
NAV_TIMEOUT_MS = 60000


class DriverError(RuntimeError):
    """A navigation that did not produce a page. `proxy_failure` names a dead
    exit (Chromium's ERR_PROXY_*/ERR_TUNNEL_* code), which wants a different
    exit rather than a retry."""

    def __init__(self, message: str, proxy_failure: str = ""):
        super().__init__(message)
        self.proxy_failure = proxy_failure


class ConnectError(RuntimeError):
    """Could not attach to --cdp-endpoint. The message is already masked."""


class LaunchError(RuntimeError):
    """The local browser would not start. Exit 1 — the environment is broken,
    not the site — but with a sentence saying what to do, not a traceback."""


class UsageError(RuntimeError):
    """A combination this engine cannot do, discovered when the driver starts
    (Selenium handed a credentialled CDP endpoint). Exit 2, with the reason."""


# Chromium's own names for "the proxy is the problem, not the site". Every
# driver surfaces them as text — in an exception, or on Chromium's own error
# page, which is what Selenium navigates to instead of raising.
PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH",
    "ERR_PROXY_CERTIFICATE_INVALID",
)


def proxy_failure_in(text: str) -> str:
    text = text or ""
    return next((m for m in PROXY_ERROR_MARKERS if m in text), "")


# Every `scheme://user:pass@` in a string, however many times it occurs. A
# Playwright connection error repeats the endpoint five times (the message
# plus a four-line call log), so a masker that handled only the first would
# print the password four times and look like it was working.
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def mask_credentials(text: str) -> str:
    """`text` with every user:pass in an embedded URL replaced; host:port kept."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards, in page order: dedupe that
    mutates a running set inside the loop makes the OUTPUT depend on arrival
    order, which is wrong the moment pages are fetched concurrently.
    """
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    # Set when the load failed because the proxy exit was unusable, rather
    # than on a timeout — the stop reason says which.
    proxy_failure: str = ""
    # The page_flow state ("content", "empty", "end", …). An "end" page and
    # an "empty" one both hold zero rows and are both correct answers; a
    # failed page holds zero rows and is not.
    state: Optional[str] = None
    # The server answered with a different page than the URL asked for.
    served_other: bool = False
    # Served (the island or product links were there) and parsed to 0 rows:
    # this parser's problem, not the end of the listing (§20).
    parsed_nothing: bool = False
    # An unexpected exception while fetching this page — recorded, masked,
    # so the pages already gathered survive it.
    error: str = ""
    # The listing's own arithmetic, from the page (see listing_info).
    listing: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None

    @property
    def ended(self) -> bool:
        """This page says the listing is over."""
        return self.ok and (self.state in ("end", "empty") or self.served_other)

    @property
    def counted(self) -> bool:
        """A page of the listing that was fetched and belongs in pages_completed."""
        return self.ok and not self.served_other and self.state != "end"


# ---------------------------------------------------------------------------
# Captcha
# ---------------------------------------------------------------------------
def handle_captcha_if_present(driver, args) -> bool:
    """Detect and solve a reCAPTCHA. True if something was solved.

    Called ONCE per fetch attempt, and a fetch has at most
    `block_retries + 1` attempts — so a page can buy at most that many
    solves, and that is the whole budget. (A sibling repo called its solver
    twice per attempt and counted only one call; one page bought three
    Turnstile solves.)

    Kohl's renders reCAPTCHA v3 on its account pages (/signin); none was
    found on any listing, search or product page captured. This path exists
    because detection stays broad — which challenge a run meets depends on
    the exit — and it does NOT touch Akamai's refusal page, which carries no
    widget at all.
    """
    html = driver.content()
    if not html:
        return False
    # Detected is not blocking: a challenge on a page whose products are
    # already readable guards nothing, and counting links is instant.
    already_rendered = driver.count(ITEM_LINK_SELECTOR)
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    challenge = reconcile_detections(detect_recaptcha_v3(html, driver.current_url()),
                                     driver.recaptcha_in_page())
    if not challenge:
        return False
    if when_blocked and already_rendered >= MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d product links are already on "
                    "the page — not solving it. Pass --solve-captcha always to "
                    "solve it anyway.", challenge.kind, challenge.source,
                    already_rendered)
        return False

    logger.warning("%s detected via %s (sitekey=%s, action=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey, challenge.action)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key is configured, so no solve is "
                       "attempted — continuing with whatever the page already holds.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                               api_version=args.captcha_api,
                               min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
        logger.error("Solving the challenge failed (%s) — continuing with "
                     "whatever the page holds.", e)
        return False
    driver.inject_token(token)
    logger.info("Token injected; the page is re-read without a reload.")
    return True


# ---------------------------------------------------------------------------
# One page
# ---------------------------------------------------------------------------
def _parse_for_mode(html: str, url: str, args) -> List:
    if args.mode == "product":
        row = parse_product_detail(html, url, category=args.category)
        return [row] if row is not None else []
    return parse_products(html, url, category=args.category)


def _save_debug(driver, args, page_num: int, html: str) -> str:
    debug_html = f"{args.out}_page{page_num}_debug.html"
    with open(debug_html, "w", encoding="utf-8") as f:
        f.write(html or "")
    try:
        driver.screenshot(f"{args.out}_page{page_num}_debug.png")
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not capture screenshot: %s", e)
    return debug_html


def fetch_one_page(driver, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Retries, rotations and debug dumps live here.

    Never raises for an EXPECTED failure — a timeout, a refusal, a challenge,
    a dead exit are all recorded on the outcome, because what the run should
    do about them differs between the sequential and concurrent paths.
    """
    outcome = PageOutcome(page_num=page_num, url=url)
    rotating = bool(pool and len(pool) > 1)
    # With a pool, a refused page is retried from OTHER exits; without one it
    # gets BLOCK_RETRIES_WITHOUT_POOL retries in a fresh session (new cookie
    # jar, same address) — see page_flow for why that number is small.
    block_retries = args.proxy_block_retries if rotating else BLOCK_RETRIES_WITHOUT_POOL
    html, state, status, load_failed = "", "blocked", None, False

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
        load_failed, exit_failed = False, ""
        for attempt in range(1, args.retries + 1):
            try:
                status = driver.goto(url)
                load_failed = False
                break
            except DriverError as e:
                load_failed = True
                if e.proxy_failure:
                    exit_failed = e.proxy_failure
                    break  # a different exit is the only thing that helps
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Could not load %s (attempt %d/%d: %s) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, mask_credentials(str(e))[:200], pause)
                    time.sleep(pause)

        if exit_failed:
            if rotating and block_attempt < block_retries:
                logger.warning("Exit %s is unusable (%s) — rotating to another one "
                               "(%d/%d).", mask(pool.current), exit_failed,
                               block_attempt + 1, block_retries)
                pool.advance(f"unusable exit: {exit_failed}")
                driver.relaunch()
                continue
            logger.error("The proxy exit is unusable (%s): the browser could not "
                         "reach kohls.com through it at all.", exit_failed)
        if load_failed:
            break

        if handle_captcha_if_present(driver, args):
            driver.sleep(1000)
            status = None  # the solve navigated; the old status is stale

        html = driver.content()
        state = page_flow.classify(html, status=status, url=driver.current_url())
        if not page_flow.should_retry(state):
            break
        if block_attempt < block_retries:
            if rotating:
                logger.warning("Page %d came back %s from %s — retrying from "
                               "another exit (%d/%d).", page_num, state,
                               mask(pool.current), block_attempt + 1, block_retries)
                pool.advance(f"{state} on page {page_num}")
            else:
                logger.warning("Page %d came back %s — retrying once more "
                               "(a fresh browser locally; a new page on the same "
                               "remote profile over --cdp-endpoint) (%d/%d).",
                               page_num, state, block_attempt + 1, block_retries)
            driver.relaunch()
            time.sleep(args.retry_delay)

    if not load_failed and state == "unloaded":
        # Still blank after every retry: the content was never obtained.
        logger.error("Page %d came back as an empty document every time — the "
                     "page never arrived (a proxy that did not authenticate "
                     "does this under Selenium). Reported as a load failure.",
                     page_num)
        load_failed = True
    if load_failed:
        logger.error("Gave up loading %s.", url)
        outcome.load_failed = True
        outcome.proxy_failure = exit_failed
        return outcome

    outcome.state = state
    outcome.final_url = driver.current_url()

    if page_flow.counts_as_blocked(state):
        vendor = detect_bot_challenge(html, url=outcome.final_url) or state
        debug_html = _save_debug(driver, args, page_num, html)
        if state == "blocked":
            logger.error(
                "kohls.com refused this request (%s, HTTP %s, %d bytes) — saved "
                "to %s. There is no challenge on that page to solve, so a "
                "2Captcha key does not help. What changed the answer in testing "
                "was the client and the exit together: the Scraping Browser API "
                "(--cdp-endpoint) was served where local Chromium, real Chrome "
                "and curl on residential exits were not. Exit 3, distinct from "
                "an empty result (exit 4).",
                vendor, status if status is not None else "n/a", len(html), debug_html)
        else:
            logger.error("Page %d is behind %s (%d bytes) — saved to %s. Exit 3, "
                         "distinct from an empty result (exit 4).", page_num,
                         vendor, len(html), debug_html)
        outcome.blocked_by = vendor
        return outcome

    if state == "end":
        logger.info("Page %d is past the end of the listing (Kohl's answered "
                    "with its 'page not available' page).", page_num)
        return outcome

    if state == "content":
        found = page_flow.wait_for_count(
            driver.count, driver.sleep, page_flow.ready_selector(args.mode),
            page_flow.min_matches(args.mode), page_flow.content_timeout_ms(args.mode))
        if not found:
            logger.info("The data island did not appear within %.0fs — parsing "
                        "what arrived (the URL-pattern fallback reads tiles).",
                        page_flow.content_timeout_ms(args.mode) / 1000)
        html = driver.content() or html

    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    if args.mode == "listing":
        outcome.listing = listing_info(html)
        if page_flow.served_other_page(url, html):
            outcome.served_other = True
            logger.warning("Asked for page %s, and the server answered with page "
                           "%s — treating that as the end of the listing, and "
                           "discarding the page rather than recording another "
                           "page's rows under this number.",
                           page_number_from_url(url),
                           outcome.listing.get("served_page"))
            return outcome

    products = _parse_for_mode(html, outcome.final_url, args)
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    if products and args.mode == "listing":
        priced = sum(1 for p in products if p.price is not None)
        logger.info("Price coverage on page %d: %d/%d (%.0f%%); source: %s.",
                    page_num, priced, len(products), 100.0 * priced / len(products),
                    ", ".join(sorted({p.price_source or "?" for p in products})))
        if priced < len(products):
            logger.warning("%d row(s) on page %d carry no price — 863 of 863 "
                           "measured rows had one. Re-run with --dump-html.",
                           len(products) - priced, page_num)

    if not products and state == "content":
        debug_html = _save_debug(driver, args, page_num, html)
        logger.warning("The page was served and parsed to 0 rows — saved to %s. "
                       "A served listing that parses to nothing is this parser's "
                       "problem, not the catalogue's.", debug_html)
        outcome.parsed_nothing = True

    outcome.products = products
    return outcome


def fetch_guarded(driver, args, pool, page_num: int, url: str, fetch=None) -> PageOutcome:
    """fetch_one_page, with an UNEXPECTED exception turned into a failed page.

    A browser can die under a run in ways no driver maps to DriverError — a
    closed target, a timeout inside a screenshot, a relaunch that cannot
    start — and letting that propagate threw away every page already
    gathered and exited 1. Recorded instead, message masked, so the run ends
    as the partial run it is (exit 6), or exit 5 if nothing was gathered.
    """
    try:
        return (fetch or fetch_one_page)(driver, args, pool, page_num, url)
    except Exception as e:  # noqa: BLE001 — recorded, not swallowed
        message = mask_credentials("%s: %s" % (type(e).__name__, e))[:300]
        logger.error("Page %d failed unexpectedly (%s) — recording it as a "
                     "failed page and keeping what was gathered.", page_num, message)
        return PageOutcome(page_num=page_num, url=url, load_failed=True, error=message)


# ---------------------------------------------------------------------------
# Many pages
# ---------------------------------------------------------------------------
def worker_pool(pool, worker_index: int):
    """A private ProxyPool for one worker, starting at a different exit, so
    workers leave from distinct addresses and no thread needs a lock."""
    if not pool:
        return None
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")


def fetch_pages_concurrently(args, pool, specs, concurrency: int,
                             make_driver: Callable, fetch: Callable = None):
    """Fetch `specs` [(page_num, url), ...] across `concurrency` workers.

    Each worker builds its OWN driver in its own thread — Playwright's sync
    API ties a browser to the thread that made it — on its own exit. Returns
    (outcomes, unattempted page numbers, whether the end was reached).
    `fetch` exists so the machinery can be tested with the browser stubbed out.
    """
    fetch = fetch or fetch_one_page
    work = queue.Queue()
    for spec in specs:
        work.put(spec)

    results = []
    results_lock = threading.Lock()
    # Set when a page says the listing is over, so asking for 50 pages of a
    # 5-page listing costs at most (concurrency - 1) extra fetches.
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        driver = None
        try:
            wpool = worker_pool(pool, index)
            driver = make_driver(args, wpool, remote=False).open()
            first = True
            while not exhausted.is_set():
                try:
                    page_num, url = work.get_nowait()
                except queue.Empty:
                    break
                if not first:
                    time.sleep(args.delay)
                first = False
                # Guarded: a worker whose browser dies mid-page records THAT
                # page as failed instead of losing it — the page was already
                # off the queue, so it was neither a result nor unattempted,
                # and the run used to finish "complete" without it.
                outcome = fetch_guarded(driver, args, wpool, page_num, url, fetch=fetch)
                with results_lock:
                    results.append(outcome)
                if outcome.error:
                    logger.error("[%s] stopping after an unexpected failure on "
                                 "page %d; its remaining pages are unattempted.",
                                 name, page_num)
                    break
                if outcome.parsed_nothing:
                    exhausted.set()
                    break
                if outcome.ended or (outcome.ok and not outcome.products):
                    logger.info("[%s] page %d ends the listing — stopping "
                                "dispatch.", name, page_num)
                    exhausted.set()
        except Exception:  # noqa: BLE001 — a dead worker must not hang the run
            logger.exception("[%s] died; its remaining pages are reported as "
                             "unattempted.", name)
        finally:
            if driver is not None:
                driver.close()

    threads = [threading.Thread(target=worker, args=(i,), name=f"page-worker-{i + 1}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    unattempted = []
    while True:
        try:
            unattempted.append(work.get_nowait()[0])
        except queue.Empty:
            break
    return results, sorted(unattempted), exhausted.is_set()


def _stop_reason_for(outcome: PageOutcome) -> str:
    if outcome.error:
        return "page_error"
    if outcome.load_failed:
        return "proxy_unusable" if outcome.proxy_failure else "page_load_timeout"
    return f"blocked_{outcome.blocked_by}"


def scrape(args, make_driver: Callable) -> int:
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    stop_reason = "single_page_mode" if args.mode != "listing" else "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None

    concurrency = max(1, args.concurrency)
    if concurrency > 1:
        if args.mode != "listing":
            logger.info("--concurrency is ignored in --mode %s: there is one "
                        "page to fetch.", args.mode)
            concurrency = 1
        elif args.cdp_endpoint:
            logger.warning("--concurrency is ignored with --cdp-endpoint: the "
                           "Scraping Browser API allows one live connection per "
                           "profile, and several workers would collide on it "
                           "(profile_locked). Use several pids, one run each.")
            concurrency = 1
        elif not pool:
            logger.warning("--concurrency %d with no proxy pool: every worker "
                           "leaves from the SAME address, which is a faster way "
                           "to get that address scored by Akamai than to gather "
                           "data. Pass --proxy-file to spread the load.", concurrency)
        if pool and pool.rotates_per_page():
            logger.info("--proxy-rotate per-page is redundant under "
                        "--concurrency: each worker already holds its own exit.")
        if concurrency > 8:
            logger.warning("--concurrency %d means %d browsers at once "
                           "(~150-300MB each).", concurrency, concurrency)

    start_page = page_number_from_url(args.url) or 1
    listing_meta: dict = {}

    driver = make_driver(args, pool, remote=bool(args.cdp_endpoint)).open()
    try:
        # Page 1 of the run is always fetched alone: it states how many pages
        # exist, which decides everything after it.
        first = fetch_guarded(driver, args, pool, 1, args.url)
        outcomes.append(first)

        if not first.ok:
            stop_reason = _stop_reason_for(first)
            blocked = first.blocked_by is not None
        elif first.parsed_nothing:
            stop_reason = "served_but_unparsed"
        elif args.mode != "listing":
            pass  # one page is the whole run
        elif first.ended:
            stop_reason = "end_of_listing"
        else:
            listing_meta = {k: v for k, v in first.listing.items()
                            if k != "served_page"}
            seen_keys.update(p.sku for p in first.products if p.sku is not None)
            total = listing_meta.get("pages_available")
            planned_pages = page_flow.pages_to_fetch(args.pages, total, start_page)
            if planned_pages < args.pages:
                logger.info("The listing has %s page(s) in total; this run "
                            "starts at page %d, so it will fetch %d, not %d.",
                            total, start_page, planned_pages, args.pages)
            urls = [page_url(args.url, start_page + k) for k in range(1, planned_pages)]

            if urls and concurrency > 1:
                driver.close()
                driver = None
                specs = [(k + 2, u) for k, u in enumerate(urls)]
                logger.info("Fetching pages 2-%d across %d workers%s.",
                            planned_pages, concurrency,
                            f" over {len(pool)} exit(s)" if pool else "")
                rest, unattempted, exhausted = fetch_pages_concurrently(
                    args, pool, specs, concurrency, make_driver)
                outcomes.extend(rest)
                failed = [o for o in rest if not o.ok]
                if failed:
                    worst = min(failed, key=lambda o: o.page_num)
                    stop_reason = _stop_reason_for(worst)
                    blocked = any(o.blocked_by for o in rest)
                elif any(o.parsed_nothing for o in rest):
                    stop_reason = "served_but_unparsed"
                elif exhausted:
                    stop_reason = "end_of_listing"
                elif unattempted:
                    # A worker died before its queue drained. Not "complete".
                    stop_reason = "pages_unattempted"
            else:
                for k, url in enumerate(urls):
                    page_num = k + 2
                    if pool and pool.rotates_per_page():
                        pool.advance(f"per-page rotation, page {page_num}")
                        try:
                            driver.relaunch()
                        except Exception as e:  # noqa: BLE001 — same rule as fetch_guarded
                            msg = mask_credentials("%s: %s" % (type(e).__name__, e))[:300]
                            logger.error("Could not relaunch for page %d (%s).", page_num, msg)
                            outcomes.append(PageOutcome(page_num=page_num, url=url,
                                                        load_failed=True, error=msg))
                            stop_reason = "page_error"
                            break
                    time.sleep(args.delay)
                    outcome = fetch_guarded(driver, args, pool, page_num, url)
                    outcomes.append(outcome)
                    if not outcome.ok:
                        stop_reason = _stop_reason_for(outcome)
                        blocked = outcome.blocked_by is not None
                        break
                    if outcome.parsed_nothing:
                        stop_reason = "served_but_unparsed"
                        break
                    if outcome.ended:
                        stop_reason = "end_of_listing"
                        break
                    fresh = sum(1 for p in outcome.products
                                if p.sku is None or p.sku not in seen_keys)
                    seen_keys.update(p.sku for p in outcome.products
                                     if p.sku is not None)
                    if not fresh:
                        logger.info("Page %d added no rows not already seen "
                                    "— treating that as the end of the listing.",
                                    page_num)
                        stop_reason = "no_new_products"
                        break
            if planned_pages < args.pages and stop_reason == "completed":
                stop_reason = "end_of_listing"
    finally:
        if driver is not None:
            driver.close()

    return merge_and_finish(args, outcomes, blocked, stop_reason, listing_meta)


def merge_and_finish(args, outcomes: List[PageOutcome], blocked: bool,
                     stop_reason: str, listing_meta: dict) -> int:
    """Merge outcomes in PAGE order, dedupe, and write through finish_run()."""
    all_rows = []
    merged_seen = set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, merged_seen, key="sku")
        if len(fresh) < len(oc.products):
            logger.info("Page %d: dropped %d row(s) already seen on an earlier "
                        "page. Kohl's default 'Featured' ordering moves while a "
                        "run is in progress; for a stable sample use a sorted "
                        "URL (e.g. &S=4, Price Low-High).",
                        oc.page_num, len(oc.products) - len(fresh))
        all_rows.extend(fresh)

    if args.mode == "listing" and all_rows and listing_meta.get("total_results"):
        logger.info("This listing holds %d product(s) in %s page(s); this run "
                    "took %d (%.1f%%).", listing_meta["total_results"],
                    listing_meta.get("pages_available"), len(all_rows),
                    100.0 * len(all_rows) / listing_meta["total_results"])

    ok_pages = [o for o in outcomes if o.counted]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url) or args.url

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, mode=args.mode,
                      source=site_host(final_url) or site_host(args.url),
                      start_url=args.url, final_url=final_url,
                      listing=listing_meta)


# ---------------------------------------------------------------------------
# CLI — one parser for all three engines, so their flags cannot drift
# ---------------------------------------------------------------------------
def build_parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--url", default=None,
                   help="kohls.com URL: a category grid "
                        "(/catalog/…jsp?CN=…), search results "
                        "(/search.jsp?search=…) or a product page "
                        "(/product/prd-NNN/…) depending on --mode. Keep the "
                        "CN= filter on a category URL: it is what selects the "
                        "category. Required unless KOHLS_URL "
                        "is set in the environment or in .env.")
    p.add_argument("--mode", choices=["listing", "product"], default="listing",
                   help="listing (default): a paginated category grid or search "
                        "results, 120 products per page. product: one "
                        "/product/ page, which adds brand and per-SKU stock "
                        "counts.")
    p.add_argument("--category", default=None,
                   help="Label to tag rows with. Defaults to the category slug "
                        "from the URL (and to the breadcrumb in --mode product). "
                        "A search URL has no category, so pass one to fill the "
                        "column on a search run.")
    p.add_argument("--pages", type=int, default=1,
                   help="Listing pages to crawl, starting at the page the URL "
                        "asks for. Capped at the listing's own totalPages. "
                        "Ignored outside --mode listing.")
    p.add_argument("--delay", type=float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Fetch pages 2..N through N parallel workers (default 1). "
                        "Each worker runs its own browser and holds its own proxy "
                        "exit. Ignored with --cdp-endpoint.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3); "
                        "the pause doubles each time. An empty or past-the-end "
                        "page is not retried — it is a correct answer.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry, doubling thereafter")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="kohls_products", help="Output file prefix")
    p.add_argument("--locale", default="en-US",
                   help="Browser locale (default en-US; kohls.com is a US shop). "
                        "It does not decide the currency, which is read from the "
                        "page's own structured data.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:PORT "
                        "(2captcha.com/proxy). Prefer KOHLS_PROXY in .env: a "
                        "password on a command line is visible to `ps`.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run",
                   help="per-run (default): one exit for the whole run. "
                        "per-page: a new exit and a fresh browser every page.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page comes back refused or challenged, retry it "
                        "from this many OTHER exits (default 2). Needs a pool of "
                        "more than one; without one a refused page gets "
                        f"{BLOCK_RETRIES_WITHOUT_POOL} retry in a fresh session.")
    p.add_argument("--twocaptcha-key", default=None,
                   help="2captcha.com API key. Prefer TWOCAPTCHA_KEY in .env.")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found. Off by "
                        "default so a failed run can't overwrite a good result.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Apply a complete browser identity from 2captcha's "
                        "Fingerprint API to the launched browser. Needs a key. "
                        "Ignored with --cdp-endpoint.")
    # ONE OS-family tag, not a list. The family shipped this as
    # "Windows,Chrome,Desktop", which the API rejects with HTTP 400, so
    # --fingerprint failed on every invocation in four repos at once;
    # fingerprint_client.py's own --tags help said so all along.
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: Windows, "
                        "Microsoft Windows or Android. NOT a list — the API "
                        "rejects Chrome, Desktop and Mobile with 400. "
                        "(default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2 (us for this "
                        "site). Match it to the proxy's exit country.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="2captcha solver API: v2 (createTask, default) or v1 "
                        "(in.php/res.php).")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a challenge "
                        "if the products are not already readable. always: solve "
                        "whenever one is detected. Neither touches Akamai's "
                        "refusal page, which carries no challenge.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score to request (0.3, 0.7 or 0.9).")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP instead "
                        "of launching one — the Scraping Browser API endpoint. "
                        "Prefer KOHLS_CDP_ENDPOINT in .env. --proxy and "
                        "--headless/--headful are ignored with it.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success as "
                        "well as failure.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    return p


def validate_args(p: argparse.ArgumentParser, args) -> None:
    """Checks shared by all three engines, after env_config has filled gaps."""
    if not args.url:
        p.error("no --url given, and KOHLS_URL is not set in the environment "
                "or in .env.")
    if not is_supported_host(args.url):
        why = unsupported_reason(args.url)
        if why:
            p.error(f"{site_host(args.url)} {why}.")
        p.error(f"{site_host(args.url) or args.url!r} is not kohls.com. "
                f"Supported hosts: {', '.join(sorted(HOSTS))}.")
    if args.pages < 1:
        p.error("--pages must be at least 1")
    if args.concurrency < 1:
        p.error("--concurrency must be at least 1")
    if args.retries < 1:
        p.error("--retries must be at least 1 (it counts attempts, not re-tries)")
    if args.proxy_block_retries < 0:
        p.error("--proxy-block-retries cannot be negative")
    if args.delay < 0 or args.retry_delay < 0:
        p.error("--delay and --retry-delay cannot be negative")
    kind = listing_kind(args.url)
    if args.mode != "listing" and args.pages != 1:
        logger.warning("--pages %d is ignored in --mode %s: there is one page "
                       "to read.", args.pages, args.mode)
        args.pages = 1
    if args.mode == "listing" and kind == "product":
        p.error("--url is a product page but --mode is listing. Use "
                "--mode product for a /product/prd-NNN/ URL.")
    if args.mode == "listing" and kind == "other":
        p.error(f"{args.url} is not a listing: use a /catalog/…jsp?CN=… category "
                f"URL or a /search.jsp?search=… URL.")
    if args.mode == "product" and kind != "product":
        p.error(f"--mode product needs a /product/prd-NNN/ URL; got {args.url}")
    if args.fingerprint and not args.twocaptcha_key:
        p.error("--fingerprint needs a 2captcha key (the Fingerprint API uses "
                "the same key, as a separate subscription).")
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own identity, and stacking "
                       "a second creates a contradiction rather than cover.")
        args.fingerprint = False


def parse_args(description: str, extra: Optional[Callable] = None, argv=None):
    p = build_parser(description)
    if extra:
        extra(p)
    args = p.parse_args(argv)
    env_config.apply(args)
    validate_args(p, args)
    return args


def main(make_driver: Callable, description: str,
         extra: Optional[Callable] = None) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    args = parse_args(description, extra)
    try:
        sys.exit(scrape(args, make_driver))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
    except LaunchError as e:
        logger.error("%s", e)
        sys.exit(1)
    except UsageError as e:
        logger.error("%s", e)
        sys.exit(2)
    except FingerprintError as e:
        logger.error("%s", e)
        sys.exit(EXIT_FETCH_FAILED)
    except ConnectError as e:
        # Could not attach to the Scraping Browser: the content was never
        # obtained, which is exit 5 in this family's contract — not a crash.
        logger.error("%s", e)
        sys.exit(EXIT_FETCH_FAILED)
