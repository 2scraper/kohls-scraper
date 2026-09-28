# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) as closely as a command-line
toolkit can: a patch release means "fixes", not that every default is
frozen — a patch that changes a default says so at the top of its entry.

## [1.0.1] - Unreleased

Fixes from a third-party audit (2026-09-27) and its re-check. Each is
pinned by a smoke_test.py check that fails on 1.0.0.

### Fixed

- **Output is published atomically.** JSON, CSV and the sidecar are written
  to temporary files beside their targets, fsync'd, and renamed into place
  together, sidecar last. A run killed mid-write, a full disk or an error in
  the CSV writer used to leave a truncated file under the real name — and
  JSON, CSV and sidecar from different runs.
- **Page numbers in the sidecar, logs and debug dumps are the listing's
  own.** A run started on `WS=120` (page 2) whose next page failed recorded
  `pages_failed: [2]` — the page that had been served — instead of `[3]`.
- **Unattempted pages are named.** A concurrent run whose workers died left
  its queued pages out of every sidecar field. They are now listed in the
  new `pages_unattempted`, and make the run partial.
- **A missing `--out` directory is refused up front (exit 2).** It used to
  turn a refused page into "exit 5, page_error" (the debug dump could not be
  written), and to crash a successful run with a traceback at the final
  write, after every page had been fetched and paid for. Same check in
  `scraper_api_client.py`.
- **A debug dump that cannot be written no longer changes the exit code.**
- **Internal errors are no longer reported as transport failures.** A
  `TypeError`, `KeyError`, `AttributeError`… inside the scraper used to be
  recorded like a dead browser (exit 5 on page 1). The run now keeps what it
  gathered (partial, stop reason `internal_error`), logs the traceback with
  credentials masked, and exits 1. Browser and driver errors keep their
  meaning.
- **`--help` no longer reads `.env`.** `env_config.py` ignored its
  arguments, so `env_config.py --help` — which CI and the offline suite run
  — loaded `.env` and printed the configuration report; `fingerprint_client.py`
  loaded `.env` before parsing arguments. Both now parse first. The report
  shows a value only for `KOHLS_URL` (an allowlist): the old rule hid a value
  only when its name held `KEY` or it contained `@`, so an endpoint carrying
  its credential as `?token=…` would have been printed.
- **`scraper_api_client.py` validates its numbers.** `--timeout` outside
  1-120 was sent to the API as-is; a negative `--retry-delay` crashed in
  `time.sleep` after a paid attempt.

### Changed

- Raw page dumps (`_debug.html`, the debug screenshot, `--dump-html`, the
  Scraper API client's dumps) are created `0600`.
- The sidecar gains `pages_unattempted` and `outputs` (the data files this
  run wrote — with `--format json`, a CSV left by an earlier run is not
  this run's). Both are additive.
- CI runs the offline suite once, not twice: the pytest step now only checks
  that the entry point collects.
- Stale docstrings corrected: `output_writer` no longer claims a listing run
  can be diffed against a product run (`diff_runs.py` refuses that), and
  `diff_runs._run_status` no longer says the Scraper API client writes no
  sidecar.

## [1.0.0] - 2026-09-25

First release, rebuilt on the 2scraper family core. It replaces an
unpublished August 2026 prototype, whose README is not carried over because
most of what it described is no longer true of the site or of the code:

> **If you used the prototype:** its flag for a "local auto-solve API" is
> gone — it called a placeholder endpoint that never existed. The parser no longer reads
> Tailwind CSS classes; it reads the listing's server-rendered data island.
> Pagination is `WS=`, verified per page, where the prototype had none that
> worked. Credentials now come from `.env`, not from the command line.

### Added

- **Three browser engines behind one bridge.** `playwright_scraper.py`,
  `puppeteer_scraper.py` and `selenium_scraper.py` share `browser_bridge.py`
  for everything they decide — retries, exit rotation, page states,
  planning, concurrency, exit codes — and each only drives its browser.
  Measured live on 2026-09-25: Playwright and pyppeteer returned the same 334
  rows for one listing, 0 differences.
- **`--mode listing`** for category grids and search results, from the
  Astro island's `catalogData` (120 products a page), with a URL-pattern
  fallback over the rendered grid that emitted 1,216 of the island's 1,223
  rows across 11 captures (7 had no rendered tile) and agreed on every one.
- **`--mode product`**: brand, price ranges, per-SKU stock counts.
- **Pagination by `WS=` offset, checked against the page the server says it
  served**, and planned against the listing's own `totalPages`.
- **`scraper_api_client.py`** (the 2Captcha Scraper API): 120 rows with
  `--use-cdp` routing through the Scraping Browser; refused without it.
- Columns beyond the family prefix: `page`, `position`, `price_max`,
  `original_price_max`, `price_label`, `sort`, `skus_total`,
  `skus_in_stock`. The sidecar records the listing's own `total_results`,
  `pages_available` and `per_page`.
- `make_fixtures.py`, which builds the offline suite's fixtures from real
  captures kept outside the repo, scrubs them, and refuses to write any that
  do not parse exactly like their originals.

### Fixed — in the family core this repo was built from

Found while bootstrapping, and worth checking in the sibling repos (verify
before patching; not every one will have them):

- `env_config.py` echoed a user's REAL value to the terminal when it was
  left inside braces in `.env` (the placeholder warning quoted whatever it
  found in `{…}`). It now names only the template's own placeholders.
- `scraper_api_client.py` logged the Scraper API's error BODY unredacted
  one screen below the `x-debug` header it did redact; the body can echo the
  `cdpurl` with its password. Also: a non-JSON answer crashed with exit 1,
  and a blank document was reported as "0 products" (exit 4) instead of
  "never obtained" (exit 5).
- The pyppeteer engine accepted `--fingerprint` and never applied it, and
  the Selenium engine read the user agent from `userAgent.value`, a key the
  Fingerprint API does not return. All three engines now build their
  identity from one command list (`fingerprint_client.cdp_identity_commands`).
  Not run live — see README.
- The pyppeteer engine never sent ANY CDP command it thought it was sending:
  pyppeteer's `CDPSession.send` returns a Future, not a coroutine, and the
  engine's loop bridge rejected it — so `Captcha.setAutoSolve` was never
  enabled (and the log blamed the endpoint) and `--fingerprint` would have
  crashed. Found by an independent review; re-run live: "enabled".
- The pyppeteer and Selenium engines set a Windows user agent on every
  platform — a bare UA override, which a sibling repo measured as served on
  navigation 1 and denied on 2-4. None of the engines overrides the UA now.
- The readiness wait handed the browser an evaluated STRING, which dies
  under a Content-Security-Policy without `unsafe-eval`; it polls
  `querySelectorAll` now, and treats reaching the threshold as success.
- `.github/ci_checks.py` taken from the newest sibling: it sees credentials
  inside JSON-escaped fixtures and skips virtualenvs by `pyvenv.cfg`.

### Known limits

- **kohls.com refused every local browser and HTTP client tested**, from
  residential US exits and datacentre addresses alike, and served the
  Scraping Browser API. See README "What you need".
- Selenium cannot use the Scraping Browser endpoint (it cannot send the
  password) nor authenticate a proxy, so it was run live on the refused
  paths only.
- `--fingerprint` was not run live: the account used has no Fingerprint API
  plan.
- This repo does not implement a solver for Akamai's behavioural challenge;
  it is detected and reported as exit 3.
