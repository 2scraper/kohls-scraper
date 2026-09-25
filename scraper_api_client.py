#!/usr/bin/env python3
"""
kohls-scraper — 2captcha Scraper API edition (fourth engine)
============================================================

A fourth way to run this scraper. Unlike playwright_scraper.py /
puppeteer_scraper.py / selenium_scraper.py, this one manages **no browser and
no CDP session of its own**: it POSTs a URL to 2captcha's separate **Scraper
API** (https://scraper.2captcha.com — a different product from the Scraping
Browser API the other three reach through --cdp-endpoint), gets HTML back over
plain HTTPS, and feeds it to this project's product_parser.

Why it could suit this site: every product on a kohls.com listing is in the
HTML that arrives (the Astro island), so nothing needs a browser to RENDER.
What decides whether a request is served is Akamai — see LIVE below for what
this path got on this site. Listing pages only; `--mode product` lives in the
browser engines.

LIVE — measured, see README "Measured" for the dated table
----------------------------------------------------------
August 2026 (the previous prototype of this repo): a plain request got HTTP
403 and a short Akamai page; routed through a Scraping Browser session
(`cdpurl`) it reached Akamai's behavioural challenge page and not past it.
Re-run on 2026-09-25 against the current code — the numbers are in the
README rather than here, because they describe a living site.

API surface used (per https://2captcha.com/scraper/scraper-api/api)
------------------------------------------------------------------
  POST https://scraper.2captcha.com/tasks/sync
    Authorization: Bearer <API_KEY>
    Content-Type: application/json
    {"task_type": "scrape", "url": ..., "data_format": "raw",
     "format": "json", "timeout": 1..120,
     "waitFor": {"text": ...},             # an OBJECT (see below)
     "cdpurl": "ws://user:pass@host:port"   # optional
    }
  -> 200 {"status": "success", "http_code": 200, "headers": {...},
          "body": "<!DOCTYPE html>..."}

Measured 2026-09-23: `waitFor` must be an OBJECT. The JSON-encoded string
form this client used to send is answered with HTTP 422 ("params.waitFor
must be an object") and is still billed ($0.0005); the same request with an
object gets HTTP 200. And `status` in the response is the API's own verdict
string ("success"), not the target's HTTP code — that is `http_code`.

The param is spelled `cdpurl` (all lowercase) while `waitFor` is camelCase —
that is the API's own inconsistency, not a typo here.

Usage
-----
    # plain HTTP, no browser anywhere (TWOCAPTCHA_KEY from .env)
    python3 scraper_api_client.py \
        --url "https://www.kohls.com/catalog/cuisinart.jsp?CN=Brand:Cuisinart"

    # routed through a Scraping Browser API session (KOHLS_CDP_ENDPOINT
    # from .env), waiting for the listing island rather than for the DOM
    python3 scraper_api_client.py \
        --url "https://www.kohls.com/catalog/cuisinart.jsp?CN=Brand:Cuisinart" \
        --use-cdp --wait-text catalogData --timeout 90

Requires: pip install -r requirements.txt
          (no playwright/selenium/pyppeteer needed for this engine)
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from typing import Optional

import requests

from product_parser import (parse_products, detect_bot_challenge,
                            detect_page_state, is_supported_host, listing_kind,
                            listing_info)
from output_writer import finish_run
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scraper_api_client")

API_BASE = "https://scraper.2captcha.com"
SYNC_ENDPOINT = f"{API_BASE}/tasks/sync"

# The API caps `timeout` at 120s and rejects bodies over 10,000 bytes.
MAX_API_TIMEOUT = 120

# Exit codes. Kept distinct from 2 (bad usage) on purpose: a remote API failing
# is not the operator passing wrong arguments, and a harness that lumps them
# together sends you looking in the wrong place. Run 7 reported `exit=2` for an
# HTTP 422 from the API — which reads as "you called it wrong".
EXIT_API_ERROR = 5

def _mask_credentials(url: str) -> str:
    """Never print a username:password embedded in a ws://... or http://... URL."""
    if "@" not in url:
        return url
    scheme_sep = url.find("://")
    if scheme_sep == -1:
        return url
    scheme, rest = url[:scheme_sep + 3], url[scheme_sep + 3:]
    _, _, host_part = rest.partition("@")
    return f"{scheme}***:***@{host_part}"


# Credentials embedded ANYWHERE in a blob of text, not just in a string that
# is entirely a URL — and every occurrence, not the first. A masker that
# handles one occurrence prints the password the other four times and looks
# like it is working.
_CREDS_IN_TEXT_RE = re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s'\"@]+@", re.IGNORECASE)
# Same shape as captcha_solver's and fingerprint_client's. A third copy is
# one too many and they should be unified in a family pass; reaching into
# another module's private name to avoid it would be worse.
_KEY_IN_TEXT_RE = re.compile(
    r"((?:client)?key|token|api[_-]?key)=([^&\s'\"]{6,})", re.IGNORECASE)


def _redact_debug_header(value: str) -> str:
    """The x-debug header, safe to log.

    SECURITY.md names this header as one of three places credentials reach a
    log unmasked, and it was logged verbatim: the API echoes back the task it
    ran, so a run driven through a credentialed CDP endpoint put that
    endpoint's username and password into the log, and a key passed as a
    query parameter would go the same way.

    Redaction rather than an allowlist of fields, deliberately: the header is
    the API's own metadata and its shape is not ours to pin, so an allowlist
    would silently drop the cost and timing figures this is logged FOR the
    first time the API adds a field.
    """
    return _KEY_IN_TEXT_RE.sub(r"\1=***",
                               _CREDS_IN_TEXT_RE.sub(r"\1***:***@", value))


def _build_wait_for(args) -> Optional[dict]:
    """`waitFor` is sent as an OBJECT. Measured 2026-09-23: the
    JSON-encoded string form is answered with HTTP 422 and still billed
    ($0.0005); the object form gets HTTP 200.

    Default (no flag): wait for the DOM. On a challenge-protected page
    that resolves instantly against the challenge page itself — which is
    exactly the trap documented in this module's docstring, so
    --wait-text/--wait-element exist to wait on something only the real
    page can contain."""
    if args.wait_text:
        return {"text": args.wait_text}
    if args.wait_element:
        return {"element": args.wait_element, "checkVisible": True}
    if args.wait_state:
        return {"state": args.wait_state}
    return None


def fetch_html(args):
    payload = {
        "task_type": "scrape",
        "url": args.url,
        "data_format": "raw",   # we want HTML; product_parser does the rest
        "format": "json",       # so we get {"status", "http_code", "headers", "body"}
        "timeout": min(args.timeout, MAX_API_TIMEOUT),
    }

    wait_for = _build_wait_for(args)
    if wait_for:
        payload["waitFor"] = wait_for
        logger.info("waitFor: %s", json.dumps(wait_for))

    if args.cdp_url:
        payload["cdpurl"] = args.cdp_url
        logger.info("Routing through an existing browser session: %s",
                    _mask_credentials(args.cdp_url))

    logger.info("POST %s (url=%s)", SYNC_ENDPOINT, args.url)
    resp = requests.post(
        SYNC_ENDPOINT,
        headers={"Authorization": f"Bearer {args.key}", "Content-Type": "application/json"},
        json=payload,
        # Give the HTTP call more headroom than the API-side task timeout,
        # otherwise a task that legitimately runs the full 120s looks like
        # a client-side network failure.
        timeout=min(args.timeout, MAX_API_TIMEOUT) + 30,
    )

    # The API returns its own per-task metadata (price, timings, status)
    # in an x-debug header — worth logging, it's the only place the real
    # cost of the call shows up.
    debug = resp.headers.get("x-debug")
    if debug:
        logger.info("x-debug: %s", _redact_debug_header(debug))

    if resp.status_code != 200:
        # 422 = task ran but errored (this is what a bad/unreachable
        # cdpurl produces: "CDP connect failed (user cdpurl) after N
        # attempts"); 402 = out of balance; 408 = sync wait exceeded.
        #
        # The body is REDACTED before it reaches the message. The API echoes
        # the task back in its errors, and the task can carry the cdpurl with
        # its password in it; the x-debug header above was already redacted
        # for exactly that reason and this line, one screen further down, was
        # not. An exception message is a log.
        raise RuntimeError(
            f"Scraper API returned HTTP {resp.status_code}: "
            f"{_redact_debug_header(resp.text[:500])}"
        )

    try:
        body = resp.json()
    except ValueError:
        raise RuntimeError("Scraper API answered HTTP 200 with a body that is "
                           "not JSON — nothing to parse.") from None
    html = body.get("body") or ""
    # The TARGET's HTTP code is `http_code`. `status` is the API's own
    # verdict string ("success", measured 2026-09-23), and passing it on
    # meant detect_page_state never saw a 403. Fall back to `status` only
    # if some response shape carries an int there.
    upstream_status = body.get("http_code")
    if not isinstance(upstream_status, int):
        legacy = body.get("status")
        upstream_status = legacy if isinstance(legacy, int) else None
    logger.info("Upstream page HTTP %s (API status %r), %d bytes of HTML.",
                upstream_status, body.get("status"), len(html))
    # The STATUS is returned alongside the HTML, not thrown away: it is one
    # of detect_page_state's signals, the same one the browser engines give
    # it through page_flow.
    return html, upstream_status


def main() -> int:
    args = parse_args()

    if not args.key:
        logger.error("No 2captcha API key. Pass --key, or better, export TWOCAPTCHA_KEY.")
        return 2

    # A challenge page is not necessarily final (see _run_once), so a
    # single attempt is not evidence. Each retry is a fresh billable task —
    # $0.0005 at the observed rate — so the default is deliberately low.
    attempts = max(1, args.retries + 1)
    for attempt in range(1, attempts + 1):
        rc = _run_once(args, attempt, attempts)
        if rc != 3 or attempt == attempts:
            return rc
        logger.info("Challenge page on attempt %d/%d — retrying in %ds.",
                    attempt, attempts, args.retry_delay)
        time.sleep(args.retry_delay)
    return rc


def _run_once(args, attempt: int = 1, attempts: int = 1) -> int:
    if attempts > 1:
        logger.info("Attempt %d/%d", attempt, attempts)

    try:
        html, upstream_status = fetch_html(args)
    except requests.RequestException as e:
        logger.error("Network error talking to the Scraper API: %s",
                     _redact_debug_header(str(e)))
        return EXIT_API_ERROR
    except RuntimeError as e:
        # HTTP 4xx/5xx from the API, including the 422 that a busy or
        # unreachable cdpurl produces.
        logger.error("%s", e)
        return EXIT_API_ERROR

    if args.dump_html:
        with open(args.dump_html, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Raw HTML written to %s", args.dump_html)

    # Same policy as the browser engines, through the same classifier.
    state = detect_page_state(html, status=upstream_status, url=args.url)
    if state == "unloaded":
        # A blank document is a page that never arrived — exit 5, as in the
        # browser engines, never "0 products" (exit 4).
        logger.error("The Scraper API returned an empty document (upstream "
                     "HTTP %s) — the page was never obtained.", upstream_status)
        return EXIT_API_ERROR
    if state in ("blocked", "challenge"):
        dump = f"{args.out}_scraperapi_debug.html"
        with open(dump, "w", encoding="utf-8") as f:
            f.write(html)
        logger.error(
            "kohls.com refused the Scraper API's request (%s, upstream HTTP %s, "
            "%d bytes) — saved to %s. Akamai decides this per client; the "
            "browser engines over --cdp-endpoint are the path measured to be "
            "served. Exit 3, distinct from an empty result (exit 4).",
            detect_bot_challenge(html) or state, upstream_status, len(html), dump)
        return 3

    vendor = detect_bot_challenge(html)
    if vendor:
        logger.error(
            "The Scraper API returned a %s bot-challenge page (%d bytes), not real content.",
            vendor, len(html),
        )
        logger.error("A challenge page is not a final answer — retry before concluding "
                     "anything (--retries). This site needs a rendered browser in the "
                     "path: pass --cdp-url, or use playwright_scraper.py / "
                     "puppeteer_scraper.py directly.")
        return 3

    # One listing page per run, finished through the SAME finish_run the
    # browser engines use, so the sidecar and the exit mapping cannot differ
    # from theirs (this client used to write no sidecar at all, and its
    # --allow-empty was dead code behind an early `return 4`).
    if state in ("end", "empty"):
        logger.info("kohls.com answered with no listing here (%s).", state)
        return finish_run([], args.out, args.format, args.allow_empty,
                          blocked=False, stop_reason="end_of_listing",
                          pages_requested=1, pages_completed=1,
                          start_url=args.url, final_url=args.url)
    products = parse_products(html, args.url, category=args.category)
    logger.info("Parsed %d products.", len(products))
    if not products:
        dump = f"{args.out}_scraperapi_debug.html"
        with open(dump, "w", encoding="utf-8") as f:
            f.write(html)
        logger.warning("0 products parsed from a served page — saved the raw "
                       "response to %s.", dump)
    info = listing_info(html)
    return finish_run(products, args.out, args.format, args.allow_empty,
                      blocked=False,
                      stop_reason="completed" if products else "served_but_unparsed",
                      pages_requested=1, pages_completed=1,
                      start_url=args.url, final_url=args.url,
                      listing={k: v for k, v in info.items() if k != "served_page"})


def parse_args():
    p = argparse.ArgumentParser(
        description="kohls.com scraper — 2captcha Scraper API edition (no "
                    "local browser). One listing page per run. Whether Akamai "
                    "serves this path is measured, not assumed: see README.")
    # NOT required: prefer the TWOCAPTCHA_KEY env var. A key passed on the
    # command line is visible to anyone who can run `ps`, and it lands in
    # shell history and in any log that echoes the command line.
    p.add_argument("--key", default=os.environ.get("TWOCAPTCHA_KEY"),
                   help="2captcha.com API key (sent as a Bearer token). "
                        "Defaults to $TWOCAPTCHA_KEY, which is the safer way to pass it.")
    p.add_argument("--url", default=None,
                   help="kohls.com listing URL (/catalog/…jsp?CN=… or "
                        "/search.jsp?search=…). Required, unless KOHLS_URL is "
                        "set in the environment or in .env.")
    p.add_argument("--category", default=None, help="Label to tag output rows with. Defaults to the category slug of a /catalog/ URL; a search URL has none, so pass one to fill the column.")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="kohls_products_scraperapi", help="Output file prefix")
    p.add_argument("--timeout", type=int, default=60,
                   help=f"API-side task timeout in seconds (1-{MAX_API_TIMEOUT}, default 60)")
    p.add_argument("--cdp-url", default=None,
                   help="Route the fetch through an existing browser session over CDP "
                        "(sent as the API's `cdpurl` param). Prefer --use-cdp, which "
                        "takes it from KOHLS_CDP_ENDPOINT so the password never "
                        "reaches a command line.")
    p.add_argument("--use-cdp", action="store_true",
                   help="Send KOHLS_CDP_ENDPOINT (from the environment or .env) as "
                        "the API's `cdpurl`. Off by default: without it this engine "
                        "does not route through the Scraping Browser even when the "
                        "variable is set, so the plain path stays measurable.")
    wait = p.add_mutually_exclusive_group()
    wait.add_argument("--wait-text", default=None,
                      help="Wait until this string appears on the page, e.g. '$'. Use this "
                           "on protected sites — a DOM/load wait is satisfied instantly by "
                           "the challenge page itself.")
    wait.add_argument("--wait-element", default=None,
                      help="Wait until this CSS selector is visible, e.g. 'a[href*=\"-item-\"]'")
    wait.add_argument("--wait-state", choices=["load", "domcontentloaded"], default=None,
                      help="Wait for a page load state instead of specific content")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 products were parsed. Off by "
                        "default so a failed fetch can't overwrite a good result.")
    p.add_argument("--retries", type=int, default=1,
                   help="Extra attempts if a bot-challenge page comes back. One retry is "
                        "usually worth it. Each attempt is a separate billable task, so "
                        "this defaults to 1.")
    p.add_argument("--retry-delay", type=int, default=10,
                   help="Seconds between retries (default 10)")
    p.add_argument("--dump-html", default=None,
                   help="Also write the raw returned HTML to this path (always, even on success)")
    args = p.parse_args()
    # This client uses --key and --cdp-url rather than --twocaptcha-key and
    # --cdp-endpoint, so the env mapping is spelled out instead of defaulted.
    keys = {"TWOCAPTCHA_KEY": "key", "KOHLS_URL": "url"}
    if args.use_cdp:
        keys["KOHLS_CDP_ENDPOINT"] = "cdp_url"
    env_config.apply(args, keys=keys)
    if not args.url:
        p.error("no --url given, and KOHLS_URL is not set in the environment "
                "or in .env.")
    if not is_supported_host(args.url):
        p.error(f"{args.url} is not a kohls.com URL.")
    if listing_kind(args.url) not in ("category", "search"):
        p.error("this engine reads one LISTING page (/catalog/…jsp?CN=… or "
                "/search.jsp?search=…); product pages live in the browser engines.")
    if args.use_cdp and not args.cdp_url:
        p.error("--use-cdp given, but KOHLS_CDP_ENDPOINT is not set.")
    return args


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
