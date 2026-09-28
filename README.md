# kohls-scraper

[![release](https://img.shields.io/github/v/release/2scraper/kohls-scraper)](https://github.com/2scraper/kohls-scraper/releases)
[![tests](https://github.com/2scraper/kohls-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/kohls-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/kohls-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/kohls-scraper/actions/workflows/canary.yml)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![licence](https://img.shields.io/badge/licence-MIT-green)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)
![access](https://img.shields.io/badge/served%20to-Scraping%20Browser%20API-orange)

Open-source scraper for **kohls.com**: category grids, search results and
product pages, as JSON and CSV — prices and price ranges, sale and clearance
labels, ratings, stock, brand and per-SKU availability. Three browser engines
(Playwright, Selenium, Puppeteer), plus the 2Captcha **Scraping Browser API**
over CDP and the 2Captcha Scraper API. Part of the
[2scraper](https://github.com/2scraper) family.

**Read [What you need](#what-you-need) first.** On 2026-09-25 kohls.com's
Akamai refused every local browser and HTTP client tried from residential US
addresses, and served the Scraping Browser API. That is a measurement, not a
pitch, and the table is below.

## Quick start

```bash
git clone https://github.com/2scraper/kohls-scraper.git
cd kohls-scraper
python3 -m venv venv
./venv/bin/pip install -r requirements.txt -r requirements-playwright.txt
cp .env.example .env         # then fill in TWOCAPTCHA_KEY and KOHLS_CDP_ENDPOINT
./venv/bin/python smoke_test.py            # offline, no network, a few seconds
./venv/bin/python env_config.py            # what was picked up, secrets hidden

./venv/bin/python playwright_scraper.py \
  --url "https://www.kohls.com/catalog/cuisinart.jsp?CN=Brand:Cuisinart" \
  --pages 3 --out cuisinart
```

`playwright install chromium` is only needed if you will launch a local
browser rather than use `--cdp-endpoint`.

## What you need

Measured 2026-09-25, one category URL, every row a separate attempt:

| Client | Exit address | Result |
|---|---|---|
| curl, own UA or a Chrome UA | 6 US residential exits | refused (403) on every one |
| Playwright's Chromium, headless | 4 US residential exits | refused (403) |
| Playwright's Chromium, headful | 4 US residential exits | refused (403) |
| real Google Chrome, headless | 2 US residential exits | refused (403) |
| real Google Chrome, headful | 2 US residential exits | HTTP 200, but the deny body once and a blank page once |
| curl, Playwright's Chromium (headless and headful) | a datacentre address (VPN) | refused |
| **Scraping Browser API**, `country-de` | its own | refused, 3 of 3 |
| **Scraping Browser API**, `country-us` | its own | refused twice, then **served** when retried later that day, and on every fetch after that |
| 2Captcha Scraper API, plain | its own | refused (upstream 403) |
| 2Captcha Scraper API, `cdpurl` = the Scraping Browser | — | **served**, 120 products |

"Refused" is Akamai's "Access Denied" page. It carries no captcha and no
challenge, so **a captcha key does not change it**: there is no widget on
that page for any solver to answer. What changed the answer was the client: the Scraping Browser API,
with a US exit. So the paid product this site needs is the **Scraping Browser
API** (`--cdp-endpoint`); captcha solving, proxies and fingerprints were not
what got a page served. A residential proxy with a local browser is still
supported and still worth a try — Akamai's scoring moves, and the table is a
day's measurement, not a law.

## What it extracts

One row per product. The first sixteen columns are the family's common
prefix, identical across every 2scraper repo.

| Column | From | Notes |
|---|---|---|
| `source` | — | `kohls.com` |
| `scraped_at` | — | UTC, ISO 8601 |
| `url`, `sku` | payload | `sku` is Kohl's `webID`, the number in `/product/prd-NNN/`; the same id on listing and product pages |
| `title` | payload | |
| `brand` | product page | **null on every listing row** — the listing payload has none (0 of 863 measured rows); `--mode product` fills it |
| `price`, `price_max` | payload | the lowest price payable now (sale if running, else regular) and the top of the range; `price_max` null for a single price |
| `currency` | the page's own JSON-LD | never a compiled-in default: null if the page states none. (On the DOM fallback only, the `$` the tile itself prints is read as USD.) |
| `original_price`, `original_price_max` | payload | the regular ("Reg."/"Orig.") price, only when a sale runs below it |
| `discount_pct` | computed | from the LOW ends of `price` and `original_price`; never read from a badge |
| `rating`, `review_count` | payload | both null when a product has no reviews (the site writes that as 0/0) |
| `in_stock` | payload | listing: ship/pickup availability — true on 863 of 863 measured rows, so a false is unobserved there; product mode: any SKU in stock |
| `image_url` | payload | |
| `category` | URL / breadcrumb | the category slug (`womens-clothing`); on a product page the deepest category crumb; null on a search unless `--category` |
| `price_source` | — | `catalog` (listing payload), `product` (product page), `dom` (fallback over rendered tiles), `jsonld` (product page without its model) |
| `page`, `position` | — | the page the SERVER served, and the position among rows this run emitted |
| `price_label` | payload | Kohl's own: `sale`, `clearance`, `sale+clearance`, `sale+regular`, … or null for a plain price |
| `sort` | payload | the ordering the site reports (`Featured`, `Price Low-High`, …) |
| `skus_total`, `skus_in_stock` | product page | how many size × colour SKUs, and how many are in stock |

A run also writes `<out>.meta.json`: status, stop reason, which pages failed
and which were never attempted (by the listing's own page number), the data
files this run wrote,
and the listing's own arithmetic (`total_results`, `pages_available`,
`per_page`, `sort`) — so "complete" is not mistaken for "exhaustive". A
3-page run of a 1,317-page category is a complete run and a 0.2% sample.

See [`sample_output.json`](sample_output.json) — eight rows cut from a real
run — and [`sample_output.meta.json`](sample_output.meta.json).

## Measured

All on 2026-09-25 over the Scraping Browser API (`country-us`), except where
the engine cannot use it.

| Run | Engine | Result |
|---|---|---|
| Cuisinart brand listing, `--pages 5` | Playwright | the site states 3 pages; 3 fetched, **334 rows = the listing's own `productCount`**, exit 0, `end_of_listing` |
| same | pyppeteer | 334 rows; `diff_runs.py` against the Playwright run: 0 added, 0 removed, 0 changed |
| Women's clothing, `&S=4`, `--pages 3` | Playwright | 357 rows; 357 priced, 315 with an original price, 38 price ranges, 250 rated; 5 sponsored and 3 "collection" entries dropped; `page`+`position` unique |
| search "coffee maker", started at `&WS=120`, `--pages 2` | Playwright | rows stamped page 2 and 3 — the server's numbers, not the loop's |
| a product page (tee) | Playwright | brand, price range, 286 SKUs of which 95 in stock; exit 0 |
| a product page (coffee maker) | pyppeteer | brand, single price, 3 SKUs; exit 0 |
| a refused page | all three | exit 3, no output written, one retry in a fresh session |
| a proxy that does not authenticate | all three | exit 5 |
| Scraper API, `--use-cdp` | — | 120 rows, $0.0005 per task (from the API's own `x-debug` header); sidecar written |
| Cuisinart again, later, default "Featured" order | pyppeteer | 337 rows fetched, 315 unique: 22 already seen on an earlier page — the ordering moved mid-run, so about 19 products were never seen |
| Cuisinart, `&S=4` (Price Low-High) | Playwright | **337 of 337** (`productCount` had grown to 337), no duplicates |
| `Captcha.setAutoSolve` over `--cdp-endpoint` | Playwright, pyppeteer | "enabled" logged by both (a first version of the pyppeteer engine never sent it — see CHANGELOG) |

Selenium was run live on the refused paths only: it cannot send the Scraping
Browser endpoint's password (see [Engines](#engines)), and every local browser
was refused. Its parsing is the same code the other two engines run.

`--fingerprint` was **not** run live: the account used had no Fingerprint API
plan (`ERROR_FINGERPRINT_PLAN_REQUIRED`). All three engines build their
identity from one shared command list, tested offline; whether each driver
applies it as intended has not been observed on a live page.

## How it reads kohls.com

- **The listing is in the first response.** kohls.com is an Astro site; every
  listing page carries its 120 products server-rendered in one
  `<astro-island>`'s props (`catalogData.products`), with the site's own
  `productCount`, `totalPages` and `currentPage`. Nothing scrolls and
  nothing waits for tiles to paint.
- **JSON-LD is a decoy for rows.** Each listing page has one `ItemList` of 15
  items against its 120 products. It is read for the currency only.
- **Pagination is `WS=` (a zero-based offset) and every page is verified.**
  The grid's own "Next Page" button produces `&WS=120&PPP=120`; a fresh
  navigation to `WS=120` is answered with `currentPage: 2`. A run plans
  against `totalPages`, and a page whose server says it is a different page
  ends the run as `end_of_listing` rather than being recorded under the wrong
  number. A page past the end is Kohl's own 404 "page not available".
- **Sponsored placements (0 to 12 a page, varying between fetches) and outfit
  "collection" entries are dropped**; `position` counts the rows emitted.
- **The default "Featured" ordering drifts.** Page 1 re-fetched 24 seconds
  later kept 115 of its 120 products; a page 1 fetched about two minutes
  before page 2 shared 30 ids with it. For monitoring, use a sorted URL (`&S=4` Price Low-High
  returned an identical page 1 twice). `diff_runs.py` refuses to compare runs
  taken under different orderings.
- **A fallback exists** for a page without the island: rendered tiles,
  anchored on the `/product/prd-<digits>/` URL pattern and scoped to the grid
  (a 24-tile recommendation carousel sits below it). On 11 captures it
  emitted 1,216 of the island's 1,223 rows — the other 7 had no rendered
  tile in the snapshot — and every one it emitted agreed with the island.

## Modes and flags

```
--mode listing    category grids (/catalog/…jsp?CN=…) and search (/search.jsp?search=…)
--mode product    one /product/prd-NNN/… page
```

**Keep the `?CN=…` filter on a category URL.** It is what selects the
category; the August prototype of this repo reported that a bare
`/catalog/…jsp` redirects to the homepage (not re-measured here, and a page
with no listing on it is exit 4 either way). Kohl's own slug-less
`/catalog.jsp?CN=…` form is accepted too.

| Flag | Default | |
|---|---|---|
| `--url` | `KOHLS_URL` | listing or product URL |
| `--pages` | 1 | listing pages from the one the URL asks for; capped at the site's `totalPages` |
| `--category` | URL slug | label for the `category` column |
| `--format` | both | `json`, `csv`, `both` |
| `--out` | `kohls_products` | output prefix |
| `--delay` / `--retries` / `--retry-delay` | 2 / 3 / 2 | seconds between pages, attempts per load, first back-off |
| `--concurrency N` | 1 | workers for pages 2..N, each with its own browser and exit; ignored with `--cdp-endpoint` (one connection per profile) |
| `--cdp-endpoint` | `KOHLS_CDP_ENDPOINT` | the Scraping Browser API |
| `--proxy` / `--proxy-file` | `KOHLS_PROXY` | one proxy / a pool to rotate |
| `--proxy-rotate` | per-run | `per-page` = a new exit and a fresh browser every page |
| `--proxy-shuffle` | off | shuffle the pool at start |
| `--proxy-block-retries` | 2 | other exits to try on a refused page (needs a pool) |
| `--twocaptcha-key` | `TWOCAPTCHA_KEY` | |
| `--solve-captcha` | when-blocked | only pay when the products are not already readable |
| `--captcha-api` / `--min-score` | v2 / 0.7 | |
| `--fingerprint` / `--fp-country` / `--fp-tags` | off / — / Windows | a complete identity from the Fingerprint API (ONE OS tag) |
| `--locale` | en-US | |
| `--allow-empty` | off | write files even for zero rows |
| `--dump-html PATH` | — | the exact HTML the parser saw, on success too |
| `--headless` / `--headful` | headless | |
| `--chromium-path` | — | **pyppeteer only**: a current Chrome instead of its old bundled one |

Credentials belong in `.env` (see [`.env.example`](.env.example)), not on the
command line, where `ps` and your shell history would keep them.

### Exit codes

`0` ok · `1` crash · `2` bad usage · `3` refused or challenged · `4` the site
answered with no products · `5` the page was never obtained (a dead proxy, a
blank document, a timeout, a CDP connection refused, a remote API failing) ·
`6` partial. A run that gathers nothing writes nothing, so last night's good
file is never replaced by an empty one — and every file is written to a
temporary name and renamed into place, so a run killed mid-write cannot
leave a truncated one either. `1` also covers an internal error (a bug here,
not the site): what was gathered is still written, marked partial with stop
reason `internal_error`, and the log carries the masked traceback.

## Engines

| Script | Engine | Scraping Browser API | Authenticated proxy |
|---|---|---|---|
| `playwright_scraper.py` | Playwright (recommended) | yes | yes |
| `puppeteer_scraper.py` | pyppeteer | yes | yes (`page.authenticate`) |
| `selenium_scraper.py` | Selenium | **no** — chromedriver's `debuggerAddress` has nowhere to put a password; refused with exit 2 | **no** — credentials are stripped, with a warning |
| `scraper_api_client.py` | none (2Captcha Scraper API) | via `--use-cdp` | — |

All three browser engines share one bridge (`browser_bridge.py`), so they
cannot disagree about retries, page states or exit codes; each only knows how
to drive its browser. Install ONE engine per virtualenv — their dependency
pins conflict (see `requirements.txt`).

**pyppeteer** is effectively unmaintained, and its bundled Chromium is old —
on an Apple-silicon Mac an x86_64 build that does not start. Pass
`--chromium-path`.

## Captchas

Kohl's renders a reCAPTCHA v3 on its account pages (`/signin`); none was found
on any listing, search or product page. The solver path (2Captcha,
`RecaptchaV3TaskProxyless` over the v2 API) runs only when a challenge is
detected on a page whose products are not already readable. Over
`--cdp-endpoint`, the Scraping Browser's `Captcha.setAutoSolve` gets the
first turn. Akamai's behavioural challenge is detected and reported (exit 3);
this repo does not implement a solver for it.

## Diffing runs

```bash
python3 diff_runs.py --old cuisinart.2026-09-24.json --new cuisinart.2026-09-25.json
```

Reports added, removed, changed (price, range, original, discount, currency,
stock) and `source_changed` — a price difference that came with a different
`price_source`, which says something about our two snapshots rather than
about the site. Refuses runs that are partial, of different modes, or under
different orderings.

## Docker

```bash
docker build -t kohls-scraper .
docker run --rm --env-file .env -v "$PWD/out:/out" kohls-scraper \
  --url "https://www.kohls.com/catalog/cuisinart.jsp?CN=Brand:Cuisinart" --pages 3 --out /out/cuisinart
```

Credentials come in at run time; the image contains none.

## Testing

`python3 smoke_test.py` — offline, no browser, no key. It pins values read off
real captures (trimmed and scrubbed by `make_fixtures.py`; the raw captures
are not in the repo), drives the whole run with the browser stubbed out, and
checks the repo itself: no credential committed, every call binding against
its callee's signature, the Docker image carrying every module it imports.
CI runs it on Python 3.9 and 3.12 and in one virtualenv per engine. The
[canary](.github/workflows/canary.yml) is a real three-page run, dispatched by
hand with a fresh `KOHLS_CDP_ENDPOINT` secret, and skips without one — a
runner's own browser is exactly what kohls.com refused.

When something goes wrong: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Legal

Scrape responsibly: respect kohls.com's terms of service and robots.txt,
rate-limit your requests, and collect only public data. This project is
provided as-is for legitimate uses such as price monitoring and market
research; how you use it is your responsibility.

## Licence

MIT — see [LICENSE](LICENSE).
