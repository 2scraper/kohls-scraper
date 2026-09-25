# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
a few seconds:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines (and redact the
Scraper API's `x-debug` header and error bodies), but two things are **not**
masked: raw HTML dumps and your shell history. Before pasting any output into
an issue or a PR, replace keys, proxy passwords and full
`ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

kohls.com changing its markup is the normal way this stops working, and it has
its own issue template. The detail that saves the most time is which path
broke, because the parser tries them in order:

1. **The listing island** — `catalogData` in the props of an
   `<astro-island>`: the whole page of products, 120 per page, plus the
   site's own `totalPages` and `currentPage`. The run log says
   `source: catalog`.
2. **The `/product/prd-<digits>/` URL pattern** over the rendered tiles,
   scoped to the grid. The run log says `source: dom`, and a run that
   suddenly says that is the report.

`--dump-html PATH` writes the exact bytes the parser was given, on success as
well as failure; a refused or empty page writes a `_debug.html` and a
screenshot on its own.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions; its fixtures are real captures, trimmed and scrubbed
by `make_fixtures.py` into `fixtures_generated.json`. The raw captures are
not in the repo on purpose — a real page carries the session that fetched it.
To add a fixture, add a capture to `../captures/` and an entry to
`make_fixtures.py`, which refuses to write anything that does not parse
exactly like its original.

Properties in this repo that exist because they were once absent, each
pinned by a test that was confirmed to go RED with the fix removed:

- **JSON-LD is not the listing.** It holds 15 items against the page's 120.
  Rows come from the island; JSON-LD supplies only the currency.
- **Sponsored and "collection" entries are not rows**, and `position` counts
  the rows emitted. The sponsored count varies between two fetches of the
  same URL, so numbering by payload slot would move every position.
- **The page number is the SERVER's.** Every listing page states which page
  it served; a page that answers with a different one ends the run as
  `end_of_listing` instead of being recorded under the wrong number.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` refused or challenged, `4` zero rows, `5` the page was never
  obtained (a dead proxy, a blank document, a timeout, the Scraper API or
  Fingerprint API failing), `6` partial. All three engines were run live on
  the paths each can take — Playwright and pyppeteer served (identical rows),
  all three refused (3) and behind an unauthenticated proxy (5) — and agree;
  Selenium cannot reach the Scraping Browser at all.
- **No marker that a served page carries.** `cf-turnstile` and
  `<captcha-widgets>` are injected into every page by the Scraping Browser's
  extension; `akam` is on every page Kohl's serves. Count a candidate on a
  good page before adding it.
- **A credential never reaches a log** — including an exception message, and
  including a value a user left inside braces in `.env`.

There is also a naming check: certain phrases are banned repo-wide and the suite
fails naming them. If it trips, read the message — the phrase is wrong for a
reason, not merely unfashionable.

### Style

- **Match the file you are editing.** No formatter is enforced.
- **Comments explain *why*.** What the code does is visible; why it does it that
  way, especially where the obvious version is wrong, is not.
- **A timeout on every remote call.** Every browser library used here has needed
  an explicit timeout its own API does not provide, and each has needed its own
  route out of the runtime — reporting a timeout is not the same as exiting on
  one. If you add a call to a remote browser or API, bound it.
- **Fail loudly.** A function that returns an empty list on error, or logs
  success without checking that the thing it wanted actually happened, is the
  single most common bug class in this codebase's history. A selector that
  matches the *wrong* element is worse than one that matches nothing, because
  the second one tells you.

### If your change needs a live run

Most do not — the suite drives the whole run with the browser stubbed out and
real pages served. If yours genuinely needs kohls.com, say in the PR what you
ran, how the page was reached (`--cdp-endpoint`, a proxy, neither), and what
you got, including the price coverage line the run prints. A run from a local
browser was refused on every address tested on 2026-09-25, so "it returned
nothing" from one is not a finding about the code.

## Scope

This repo reads **public pages** on kohls.com — category grids, search results
and product pages — as an anonymous visitor is served them. Out of scope:
anything behind a login (the account pages are where the site's reCAPTCHA
lives), anything that submits a form, and anything that defeats a protection
rather than being served the way an ordinary browser is.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
