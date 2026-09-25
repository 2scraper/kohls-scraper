#!/usr/bin/env python3
"""
kohls-scraper — Playwright edition (primary engine)
===================================================

Scrapes kohls.com category grids, search results and product pages.

    --mode listing   (default)  category grids (/catalog/…jsp?CN=…) and search
                                results (/search.jsp?search=…), 120 per page
    --mode product              one /product/prd-NNN/… page, adding brand and
                                per-SKU stock counts

Everything a run DECIDES lives in browser_bridge.py, shared by all three
engines; this file only knows how to ask Playwright.

What is different about kohls.com
---------------------------------
* **Akamai decides whether you get a page at all, and the answer depended on
  the client as much as on the address.** On 2026-09-25 the same URL was
  refused ("Access Denied", an Akamai reference id, nothing on the page to
  solve) to curl on six US residential exits, to this engine's own
  Chromium headless and headful, and to real Chrome — and served, 120
  products a page, through the 2Captcha Scraping Browser API.
* **Everything is in the first response.** The listing's 120 products are
  server-rendered into one Astro island, so nothing scrolls and nothing
  waits for tiles to paint.
* **Pages are addressed, and every page is checked** against the page the
  server says it served.

Usage
-----
    python playwright_scraper.py \\
        --url "https://www.kohls.com/catalog/womens-clothing.jsp?CN=Gender:Womens+Department:Clothing" \\
        --pages 3 --format both

    python playwright_scraper.py --mode product \\
        --url "https://www.kohls.com/product/prd-2977125/cuisinart-14-cup-programmable-coffee-maker.jsp"

Requires: pip install -r requirements.txt -r requirements-playwright.txt
          then: playwright install chromium   (only if NOT using --cdp-endpoint)
"""

import logging

# Module level on purpose: with the driver absent, importing this module must
# FAIL, so the offline suite records a skip instead of passing an engine it
# never loaded.
from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

import browser_bridge
from browser_bridge import DriverError, ConnectError, LaunchError, mask_credentials
from captcha_solver import detect_recaptcha_in_page, INJECT_TOKEN_JS
from proxy_pool import to_playwright, mask

logger = logging.getLogger("playwright_scraper")

# Playwright's words for "the document changed under this call". Akamai's
# challenge script ends in `location.reload(true)` once it is satisfied, so a
# DOM call can land in the middle of a navigation the site started itself —
# the first live run of this engine died with exit 1 on exactly that, inside
# the captcha check.
_NAVIGATING = ("navigating", "execution context was destroyed",
               "most likely because of a navigation")


class PlaywrightDriver:
    name = "playwright"

    def __init__(self, args, pool, remote: bool = False):
        self.args, self.pool, self.remote = args, pool, remote
        self._pw = self.browser = self.context = self.page = None

    # --- lifecycle -----------------------------------------------------
    def open(self):
        # One Playwright instance per driver, started in the calling thread:
        # the sync API ties a browser to the thread that created it, and a
        # concurrent worker builds its own driver in its own thread.
        self._pw = sync_playwright().start()
        if self.remote:
            self._connect_remote()
        else:
            self._launch_local()
        return self

    def _launch_local(self):
        """Our own Chromium on the pool's current exit.

        No user-agent override. The family default set a Windows Chrome UA on
        every platform; a UA that contradicts the platform, the client hints
        and the TLS handshake under it is itself the signal a bot manager
        keys on (a sibling repo measured nav 1 served and navs 2-4 denied
        under a bare UA override). A complete identity is --fingerprint.
        """
        launch_kwargs = {"headless": self.args.headless}
        proxy = to_playwright(self.pool.current) if self.pool else None
        if proxy:
            # Credentials ride in Playwright's own username/password fields,
            # never in --proxy-server= on the browser's command line.
            launch_kwargs["proxy"] = proxy
            logger.info("Using proxy exit %s", mask(self.pool.current))
        try:
            self.browser = self._pw.chromium.launch(**launch_kwargs)
        except (PWError, PWTimeout) as e:
            self._pw.stop()
            raise LaunchError(
                f"Playwright could not start Chromium ({mask_credentials(str(e)).splitlines()[0][:200]}). "
                f"If it is not installed: `playwright install chromium`.") from None
        ctx_kwargs = {"locale": self.args.locale}
        init_script = None
        if self.args.fingerprint:
            from fingerprint_client import (get_fingerprint,
                                            playwright_context_kwargs,
                                            playwright_init_script)
            fp = get_fingerprint(self.args.twocaptcha_key,
                                 tags=self.args.fp_tags, country=self.args.fp_country)
            ctx_kwargs.update(playwright_context_kwargs(fp))
            init_script = playwright_init_script(fp)
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))
        self.context = self.browser.new_context(**ctx_kwargs)
        if init_script:
            self.context.add_init_script(init_script)
        self.page = self.context.new_page()

    def _connect_remote(self):
        logger.info("Connecting to existing browser over CDP: %s",
                    mask_credentials(self.args.cdp_endpoint))
        try:
            self.browser = self._pw.chromium.connect_over_cdp(
                self.args.cdp_endpoint, timeout=30000)
        except (PWError, PWTimeout) as e:
            # Playwright repeats the endpoint — password included — in the
            # message and in its call log. Masked, host kept.
            self._pw.stop()
            raise ConnectError(
                f"could not connect to --cdp-endpoint "
                f"{mask_credentials(self.args.cdp_endpoint)}: "
                f"{mask_credentials(str(e))}\n"
                f"A Scraping Browser profile allows ONE live connection at a "
                f"time, so a 500 here usually means another run still holds "
                f"this `pid`. Wait for it to finish, or use a different pid."
            ) from None
        self.context = (self.browser.contexts[0] if self.browser.contexts
                        else self.browser.new_context())
        self.page = self.context.new_page()
        self._enable_autosolve()

    def _enable_autosolve(self):
        # The Scraping Browser API's documented CDP domain for solving
        # captchas inside the browser (https://2captcha.com/scraper/browser-api/api).
        try:
            cdp = self.context.new_cdp_session(self.page)
            cdp.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
            cdp.on("Captcha.detected", lambda *_: logger.info("[Scraping Browser] CAPTCHA detected on page."))
            cdp.on("Captcha.solveFinished", lambda *_: logger.info("[Scraping Browser] CAPTCHA solved automatically."))
            cdp.on("Captcha.solveFailed", lambda *_: logger.warning("[Scraping Browser] CAPTCHA auto-solve failed."))
            logger.info("Scraping Browser API Captcha.setAutoSolve enabled.")
        except Exception as e:  # noqa: BLE001
            logger.info("Captcha.setAutoSolve not available on this endpoint (%s).",
                        mask_credentials(str(e)))

    def relaunch(self):
        """A fresh session. Remote: a new page on the same profile (its exit is
        not ours to change). Local: a whole new browser on the pool's current
        exit — never a proxy swapped under a live session."""
        if self.remote:
            try:
                self.page.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("Ignoring error while closing page: %s", e)
            self.page = self.context.new_page()
            self._enable_autosolve()
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001 — teardown must not mask why we're here
            logger.debug("Ignoring error while closing browser: %s", e)
        self._launch_local()

    def close(self):
        try:
            if self.remote:
                self.page.close()  # leave the remote browser running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during teardown: %s", e)
        try:
            if self._pw:
                self._pw.stop()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error stopping Playwright: %s", e)

    # --- operations ----------------------------------------------------
    def _settling(self, fn, default=None, attempts: int = 6):
        for attempt in range(1, attempts + 1):
            try:
                return fn()
            except PWError as e:
                if not any(w in str(e).lower() for w in _NAVIGATING):
                    raise
                if attempt == attempts:
                    logger.warning("Page kept navigating through %d attempts.", attempts)
                    return default
                try:
                    self.page.wait_for_load_state("domcontentloaded", timeout=10000)
                except (PWError, PWTimeout):
                    self.page.wait_for_timeout(700)
        return default

    def goto(self, url):
        try:
            response = self.page.goto(url, wait_until="domcontentloaded",
                                      timeout=browser_bridge.NAV_TIMEOUT_MS)
        except (PWTimeout, PWError) as e:
            raise DriverError(mask_credentials(str(e)),
                              browser_bridge.proxy_failure_in(str(e))) from None
        return response.status if response else None

    def content(self):
        return self._settling(self.page.content, default="") or ""

    def count(self, selector):
        return self._settling(lambda: len(self.page.query_selector_all(selector)),
                              default=0)

    def current_url(self):
        return self.page.url

    def sleep(self, ms):
        self.page.wait_for_timeout(ms)

    def screenshot(self, path):
        self.page.screenshot(path=path, full_page=True)

    def recaptcha_in_page(self):
        return detect_recaptcha_in_page(
            lambda js: self._settling(lambda: self.page.evaluate(js)),
            page_url=self.page.url)

    def inject_token(self, token):
        # No reload afterwards. The token lives in the page's
        # g-recaptcha-response field and in the callbacks just invoked; a
        # reload discards both, so the paid solve could never take effect.
        # Not exercised live on kohls.com: no listing, search or product
        # page carried a reCAPTCHA (see captcha_solver.py).
        self.page.evaluate(INJECT_TOKEN_JS, token)
        self.page.wait_for_timeout(3000)


def make_driver(args, pool, remote: bool = False) -> PlaywrightDriver:
    return PlaywrightDriver(args, pool, remote=remote)


if __name__ == "__main__":
    browser_bridge.main(make_driver, "kohls.com scraper (Playwright edition)")
