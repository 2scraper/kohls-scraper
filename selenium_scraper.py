#!/usr/bin/env python3
"""
kohls-scraper — Selenium edition
================================

Same CLI, same rows, same exit codes as playwright_scraper.py: everything a
run DECIDES lives in browser_bridge.py, and this file only knows how to ask
Selenium. See playwright_scraper.py for what is different about kohls.com.

Two limits of Selenium itself, stated here rather than left to be found
-----------------------------------------------------------------------
* **Selenium cannot use an authenticated remote CDP endpoint.** Playwright's
  `connect_over_cdp` and pyppeteer's `browserWSEndpoint` take a full
  `ws://user:pass@host:port` and authenticate on the WebSocket upgrade;
  chromedriver's `debuggerAddress` takes a bare `host:port` with nowhere to
  put a password. So the Scraping Browser API — the one client measured to
  be served by kohls.com — is not reachable from this engine, and such an
  endpoint is refused up front with exit 2 rather than failing obscurely.
* **Selenium cannot authenticate a proxy.** `--proxy-server=` takes an
  address only. Credentials are stripped and a warning says so.

A third thing Selenium does differently, which the code below handles: a
navigation through a dead proxy does not raise. Chromium shows its own error
page (chrome-error://chromewebdata/) and Selenium returns normally. That page
is recognised here and reported as the unusable exit it is — otherwise this
engine would call it "blocked" (exit 3) where its twins report a load
failure (exit 5) for the same proxy.

Usage
-----
    python selenium_scraper.py \\
        --url "https://www.kohls.com/catalog/cuisinart.jsp?CN=Brand:Cuisinart" --pages 3

Requires: pip install -r requirements.txt -r requirements-selenium.txt
          and Google Chrome. Selenium Manager fetches a matching chromedriver.
"""

import logging
import time
from urllib.parse import urlsplit

# Module level on purpose: with the driver absent, importing this module must
# FAIL, so the offline suite records a skip instead of passing an engine it
# never loaded.
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

import browser_bridge
from browser_bridge import (DriverError, UsageError, LaunchError, ConnectError,
                            mask_credentials)
from captcha_solver import detect_recaptcha_in_page, INJECT_TOKEN_JS
from proxy_pool import split_credentials, mask

logger = logging.getLogger("selenium_scraper")

PAGE_LOAD_TIMEOUT = browser_bridge.NAV_TIMEOUT_MS / 1000
SCRIPT_TIMEOUT = 30


def cdp_host_port(endpoint: str) -> str:
    """`host:port` for chromedriver's debuggerAddress, or UsageError with the reason."""
    parts = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
    if parts.username or parts.password:
        raise UsageError(
            f"This --cdp-endpoint carries credentials "
            f"({mask_credentials(endpoint)}), and Selenium cannot send them: "
            f"chromedriver's debuggerAddress is a bare host:port. Use "
            f"playwright_scraper.py or puppeteer_scraper.py for the Scraping "
            f"Browser API — both authenticate on the WebSocket upgrade. (If "
            f"KOHLS_CDP_ENDPOINT is set in .env and you want a LOCAL Chrome "
            f"here, run with KOHLS_CDP_ENDPOINT= in front of the command.)")
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.hostname or endpoint}{port}"


class SeleniumDriver:
    name = "selenium"

    def __init__(self, args, pool, remote: bool = False):
        self.args, self.pool, self.remote = args, pool, remote
        self.driver = None

    # --- lifecycle -----------------------------------------------------
    def open(self):
        options = Options()
        if self.remote:
            options.debugger_address = cdp_host_port(self.args.cdp_endpoint)
            logger.info("Attaching to an existing browser at %s.",
                        options.debugger_address)
            try:
                self.driver = webdriver.Chrome(options=options)
            except WebDriverException as e:
                raise ConnectError(
                    f"could not attach to the browser at {options.debugger_address}: "
                    f"{mask_credentials((e.msg or str(e)).splitlines()[0])[:200]}") from None
            self._apply_timeouts()
            return self

        if self.args.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1600,1000")
        options.add_argument(f"--lang={self.args.locale}")
        if self.pool:
            scrubbed, credentials = split_credentials(self.pool.current)
            options.add_argument(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(self.pool.current))
            if credentials:
                logger.warning(
                    "This proxy has credentials and SELENIUM CANNOT SEND THEM: "
                    "--proxy-server accepts an address only. They have been "
                    "stripped, so the exit will most likely refuse the "
                    "connection. Use playwright_scraper.py or "
                    "puppeteer_scraper.py for an authenticated proxy.")
        # No user-agent override — see playwright_scraper.py for why.
        try:
            self.driver = webdriver.Chrome(options=options)
        except WebDriverException as e:
            raise LaunchError(
                f"Selenium could not start Chrome ({mask_credentials((e.msg or str(e)).splitlines()[0])[:200]}). "
                f"It needs Google Chrome installed; Selenium Manager fetches "
                f"the matching chromedriver.") from None
        self._apply_timeouts()
        if self.args.fingerprint:
            self._apply_fingerprint()
        return self

    def _apply_timeouts(self):
        # "Every remote call is bounded" applies to this engine too.
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        self.driver.set_script_timeout(SCRIPT_TIMEOUT)

    def _apply_fingerprint(self):
        from fingerprint_client import get_fingerprint, cdp_identity_commands
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        for method, params in cdp_identity_commands(fp):
            self.driver.execute_cdp_cmd(method, params)
        logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))

    def relaunch(self):
        if self.remote:
            return  # the attached browser is not ours to restart
        self.close()
        self.open()

    def close(self):
        try:
            if self.driver is not None:
                # quit(), not close(): close() ends a window and leaves the
                # chromedriver process running — one leaked per rotation.
                self.driver.quit()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during driver teardown: %s", e)
        self.driver = None

    # --- operations ----------------------------------------------------
    def goto(self, url):
        try:
            self.driver.get(url)
        except TimeoutException as e:
            raise DriverError(f"page load timed out: {mask_credentials(e.msg or '')}") from None
        except WebDriverException as e:
            raise DriverError(mask_credentials(e.msg or str(e)),
                              browser_bridge.proxy_failure_in(str(e))) from None
        current = self.driver.current_url or ""
        if current.startswith("chrome-error://"):
            # Chromium's own error page, which Selenium returns instead of
            # raising. Its body names the net error.
            body = self.content()
            raise DriverError(f"Chromium error page for {url}",
                              browser_bridge.proxy_failure_in(body)
                              or ("ERR_PROXY_CONNECTION_FAILED" if self.pool else ""))
        # Selenium exposes no HTTP status. page_flow copes: the refusal is
        # recognised by its own markers, and the structural "built from
        # Kohl's assets" test covers a page that carries none.
        return None

    def content(self):
        for _ in range(4):
            try:
                return self.driver.page_source or ""
            except WebDriverException:
                time.sleep(0.7)
        return ""

    def count(self, selector):
        try:
            return len(self.driver.find_elements(By.CSS_SELECTOR, selector))
        except WebDriverException:
            return 0

    def current_url(self):
        return self.driver.current_url

    def sleep(self, ms):
        time.sleep(ms / 1000.0)

    def screenshot(self, path):
        self.driver.save_screenshot(path)

    def recaptcha_in_page(self):
        # execute_script runs a function BODY and needs an explicit return,
        # unlike the arrow-function form the other two drivers pass.
        return detect_recaptcha_in_page(
            lambda js: self.driver.execute_script(f"return ({js})();"),
            page_url=self.driver.current_url)

    def inject_token(self, token):
        # No reload afterwards. The token lives in the page's
        # g-recaptcha-response field and in the callbacks just invoked; a
        # reload discards both, so the paid solve could never take effect.
        # Not exercised live on kohls.com: no listing, search or product
        # page carried a reCAPTCHA (see captcha_solver.py).
        self.driver.execute_script(f"return ({INJECT_TOKEN_JS})(arguments[0]);", token)
        time.sleep(3.0)


def make_driver(args, pool, remote: bool = False) -> SeleniumDriver:
    return SeleniumDriver(args, pool, remote=remote)


if __name__ == "__main__":
    browser_bridge.main(make_driver, "kohls.com scraper (Selenium edition). Cannot "
                                     "authenticate a proxy or a remote CDP endpoint; "
                                     "see the module docstring.")
