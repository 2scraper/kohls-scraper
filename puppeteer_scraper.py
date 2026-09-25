#!/usr/bin/env python3
"""
kohls-scraper — Puppeteer (pyppeteer) edition
=============================================

Same CLI, same rows, same exit codes as playwright_scraper.py: everything a
run DECIDES lives in browser_bridge.py, and this file only knows how to ask
pyppeteer. See playwright_scraper.py for what is different about kohls.com.

pyppeteer-specific, worth knowing before you choose it
------------------------------------------------------
* **pyppeteer is effectively unmaintained**, and its own README points at
  Playwright. Its bundled Chromium is old (revision 1181205 with pyppeteer
  2.0). `--chromium-path` points it at a newer one — the only flag this
  engine has that its twins do not.
* It authenticates a proxy properly: the address goes on `--proxy-server=`
  and the credentials through `page.authenticate`, never onto the browser's
  command line where `ps` would show them.
* pyppeteer is asyncio; the bridge is synchronous. Every call is run on a
  dedicated event-loop thread with an enforced timeout, because pyppeteer's
  own API offers none.

Usage
-----
    python puppeteer_scraper.py \\
        --url "https://www.kohls.com/catalog/cuisinart.jsp?CN=Brand:Cuisinart" --pages 3

Requires: pip install -r requirements.txt -r requirements-puppeteer.txt
"""

import asyncio
import concurrent.futures
import logging
import threading
from typing import Optional

# Module level on purpose: with the driver absent, importing this module must
# FAIL, so the offline suite records a skip instead of passing an engine it
# never loaded (a sibling imported pyppeteer inside the launch path, and its
# engine group never skipped with pyppeteer absent).
from pyppeteer import connect, launch

import browser_bridge
from browser_bridge import DriverError, ConnectError, LaunchError, mask_credentials
from captcha_solver import detect_recaptcha_in_page, INJECT_TOKEN_JS
from proxy_pool import split_credentials, mask

logger = logging.getLogger("puppeteer_scraper")

OP_TIMEOUT = 90.0        # any single pyppeteer call
CONNECT_TIMEOUT = 45.0   # connect()/launch()

_TEARDOWN_NOISE = ("Target closed", "Connection closed",
                   "No session with given id",
                   "Task was destroyed but it is pending")
_NAVIGATING = ("execution context was destroyed", "cannot find context",
               "most likely because of a navigation", "navigating")


class _AsyncBridge:
    """Runs coroutines on one dedicated event-loop thread, with a timeout.

    `.result(timeout)` returns control even when the browser never answers,
    which pyppeteer's own API does not offer.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="pyppeteer-loop")
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        # pyppeteer leaves CDP calls in flight when a browser closes, and the
        # loop logs each as "Future exception was never retrieved … Target
        # closed" at ERROR level AFTER a successful run has printed its
        # results. Only that shape is swallowed.
        self.loop.set_exception_handler(self._on_loop_exception)
        self.loop.run_forever()

    @staticmethod
    def _on_loop_exception(loop, context):
        message = str(context.get("exception") or context.get("message") or "")
        # "No session with given id" is the same class on kohls.com: the page
        # opens and closes ad iframes as it loads, and pyppeteer's detach from
        # each one lands after the target is gone — six ERROR lines on a
        # clean, complete three-page run before this was matched.
        if any(t in message for t in _TEARDOWN_NOISE):
            logger.debug("Ignoring teardown noise from pyppeteer: %s", message)
            return
        loop.default_exception_handler(context)

    def run(self, coro, timeout: Optional[float] = OP_TIMEOUT):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"pyppeteer call did not return within {timeout}s")

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)


class PuppeteerDriver:
    name = "pyppeteer"

    def __init__(self, args, pool, remote: bool = False):
        self.args, self.pool, self.remote = args, pool, remote
        self.bridge = None
        self.browser = self.page = None

    # --- lifecycle -----------------------------------------------------
    def open(self):
        if self.bridge is None:
            self.bridge = _AsyncBridge()
        if self.remote:
            self._connect_remote()
        else:
            self._launch_local()
        return self

    def _connect_remote(self):
        logger.info("Connecting to existing browser over CDP: %s",
                    mask_credentials(self.args.cdp_endpoint))
        try:
            # browserWSEndpoint takes the full ws://user:pass@host form and
            # authenticates on the WebSocket upgrade — unlike Selenium's
            # debuggerAddress, which has nowhere to put a password.
            self.browser = self.bridge.run(
                connect(browserWSEndpoint=self.args.cdp_endpoint,
                        ignoreHTTPSErrors=True), timeout=CONNECT_TIMEOUT)
        except Exception as e:  # noqa: BLE001 — any failure here is "could not attach"
            self.bridge.close()
            self.bridge = None
            raise ConnectError(
                f"could not connect to --cdp-endpoint "
                f"{mask_credentials(self.args.cdp_endpoint)}: "
                f"{type(e).__name__}: {mask_credentials(str(e))}\n"
                f"A Scraping Browser profile allows ONE live connection at a "
                f"time; another run may still hold this `pid`.") from None
        self.page = self.bridge.run(self.browser.newPage())
        self._enable_autosolve()

    def _send(self, session, method, params):
        """A CDP command through pyppeteer, run on the loop thread.

        pyppeteer's `CDPSession.send` is a plain function returning a
        Future, not a coroutine, and `run_coroutine_threadsafe` refuses a
        Future ("A coroutine object is required"). Measured the hard way:
        every live pyppeteer run logged Captcha.setAutoSolve as "not
        available on this endpoint" when it had never been sent at all.
        """
        async def call():
            return await session.send(method, params)
        return self.bridge.run(call())

    def _enable_autosolve(self):
        try:
            cdp = self.bridge.run(self.page.target.createCDPSession())
            self._send(cdp, "Captcha.setAutoSolve",
                       {"autoSolve": True, "options": [{"type": "*"}]})
            logger.info("Scraping Browser API Captcha.setAutoSolve enabled.")
        except Exception as e:  # noqa: BLE001
            logger.info("Captcha.setAutoSolve was not enabled (%s: %s).",
                        type(e).__name__, mask_credentials(str(e)))

    def _launch_local(self):
        """Our own Chromium. No user-agent override — see playwright_scraper.

        A sibling repo measured exactly this engine's old behaviour — a UA set
        through pyppeteer's CDP override — as nav 1 served and navs 2-4
        DENIED, including with a UA that matched the real platform.
        """
        launch_args = ["--no-sandbox", "--disable-dev-shm-usage",
                       f"--lang={self.args.locale}"]
        launch_kwargs = {}
        if getattr(self.args, "chromium_path", None):
            launch_kwargs["executablePath"] = self.args.chromium_path
            logger.info("Using the Chromium at %s instead of pyppeteer's own.",
                        self.args.chromium_path)
        credentials = None
        if self.pool:
            scrubbed, credentials = split_credentials(self.pool.current)
            # The address on the command line, the credentials through
            # page.authenticate(): argv is readable by anything that runs `ps`.
            launch_args.append(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(self.pool.current))
        # handleSIGINT/TERM/HUP off: pyppeteer installs signal handlers inside
        # launch(), and signal.signal raises off the main thread — the event
        # loop lives on a worker thread. Teardown is close()'s job.
        try:
            self.browser = self.bridge.run(
                launch(headless=self.args.headless, args=launch_args,
                       ignoreHTTPSErrors=True, handleSIGINT=False,
                       handleSIGTERM=False, handleSIGHUP=False, **launch_kwargs),
                timeout=CONNECT_TIMEOUT * 2)
        except Exception as e:  # noqa: BLE001 — BrowserError, TimeoutError, OSError
            # Measured on an Apple-silicon Mac, 2026-09-25: pyppeteer 2.0's
            # downloaded Chromium (r1181205) is an x86_64 build and dies on
            # launch with "Browser closed unexpectedly". Nothing about the
            # site; point it at a current Chrome instead.
            raise LaunchError(
                f"pyppeteer could not start its browser ({type(e).__name__}: "
                f"{str(e).strip()[:200]}). Its own Chromium is old and, on an "
                f"Apple-silicon Mac, an x86_64 build that does not start. Pass "
                f"--chromium-path with a current Chrome or Chromium, e.g. "
                f"\"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome\"."
            ) from None
        self.page = self.bridge.run(self.browser.newPage())
        if credentials:
            self.bridge.run(self.page.authenticate(
                {"username": credentials[0], "password": credentials[1]}))
        if self.args.fingerprint:
            self._apply_fingerprint()

    def _apply_fingerprint(self):
        from fingerprint_client import get_fingerprint, cdp_identity_commands
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        client = self.page._client  # the page's own CDP session
        for method, params in cdp_identity_commands(fp):
            self._send(client, method, params)
        logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))

    def relaunch(self):
        if self.remote:
            try:
                self.bridge.run(self.page.close(), timeout=30)
            except Exception as e:  # noqa: BLE001
                logger.debug("Ignoring error while closing page: %s", e)
            self.page = self.bridge.run(self.browser.newPage())
            self._enable_autosolve()
            return
        try:
            self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001 — teardown must not mask why we're here
            logger.debug("Ignoring error while closing browser: %s", e)
        self._launch_local()

    def close(self):
        try:
            if self.remote:
                self.bridge.run(self.page.close(), timeout=30)
                # disconnect(), not close(): leave the remote browser running.
                self.bridge.run(self.browser.disconnect(), timeout=30)
            else:
                self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during teardown: %s", e)
        if self.bridge:
            self.bridge.close()
            self.bridge = None

    # --- operations ----------------------------------------------------
    def _settling(self, coro_fn, default=None, attempts: int = 6):
        for attempt in range(1, attempts + 1):
            try:
                return self.bridge.run(coro_fn())
            except Exception as e:  # noqa: BLE001 — pyppeteer raises plain NetworkError
                if not any(w in str(e).lower() for w in _NAVIGATING):
                    raise
                if attempt == attempts:
                    logger.warning("Page kept navigating through %d attempts.", attempts)
                    return default
                self.sleep(700)
        return default

    def goto(self, url):
        try:
            response = self.bridge.run(
                self.page.goto(url, waitUntil="domcontentloaded",
                               timeout=browser_bridge.NAV_TIMEOUT_MS),
                timeout=browser_bridge.NAV_TIMEOUT_MS / 1000 + 15)
        except Exception as e:  # noqa: BLE001 — PageError, TimeoutError, NetworkError
            raise DriverError(mask_credentials(f"{type(e).__name__}: {e}"),
                              browser_bridge.proxy_failure_in(str(e))) from None
        return response.status if response else None

    def content(self):
        return self._settling(self.page.content, default="") or ""

    def count(self, selector):
        found = self._settling(lambda: self.page.querySelectorAll(selector), default=[])
        return len(found or [])

    def current_url(self):
        return self.page.url

    def sleep(self, ms):
        self.bridge.run(asyncio.sleep(ms / 1000.0), timeout=ms / 1000.0 + 10)

    def screenshot(self, path):
        self.bridge.run(self.page.screenshot({"path": path, "fullPage": True}))

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
        self.bridge.run(self.page.evaluate(INJECT_TOKEN_JS, token))
        self.sleep(3000)


def make_driver(args, pool, remote: bool = False) -> PuppeteerDriver:
    return PuppeteerDriver(args, pool, remote=remote)


def _extra_flags(p):
    p.add_argument("--chromium-path", default=None, metavar="PATH",
                   help="Chromium/Chrome executable to drive instead of "
                        "pyppeteer's own (which is old). Only this engine has "
                        "this flag; its twins manage their own browser.")


if __name__ == "__main__":
    browser_bridge.main(make_driver, "kohls.com scraper (pyppeteer edition)",
                        extra=_extra_flags)
