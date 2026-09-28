# Troubleshooting

Every entry here was met while building and running this repo against
kohls.com on 2026-09-25, with the measurement that settled it. Start with the
exit code: it says which of these you are in.

| Exit | Means | Go to |
|---|---|---|
| 0 | rows written, run complete | — |
| 2 | bad usage (the message says what) | [Usage errors](#usage-errors) |
| 3 | refused or challenged | [Exit 3](#exit-3-refused) |
| 4 | the site answered, and the answer was "no products" | [Exit 4](#exit-4-no-products) |
| 5 | the page was never obtained | [Exit 5](#exit-5-never-obtained) |
| 6 | some pages gathered, then the run stopped | [Exit 6](#exit-6-partial) |
| 1 | a crash — please report it | [Crashes](#crashes) |

## Exit 3: refused

The log names what refused it.

**`akamai-deny`** — Akamai's "Access Denied" page (`Reference #18.…`,
`errors.edgesuite.net`). It carries no widget for any solver to answer, so a
captcha key does not change it. Measured 2026-09-25, same category URL:

| Client | Exit | Result |
|---|---|---|
| curl, own UA or a Chrome UA | 6 US residential exits | refused (403) on every one |
| Playwright's Chromium, headless | 4 US residential exits | refused (403) |
| Playwright's Chromium, headful | 4 US residential exits | refused (403) |
| real Google Chrome, headless | 2 US residential exits | refused (403) |
| real Google Chrome, headful | 2 US residential exits | HTTP **200**: the deny body once, a blank document once |
| curl, Playwright's Chromium (headless and headful) | a datacentre address (VPN) | refused |
| Scraping Browser API, `country-de` | its own | refused, 3 of 3 |
| Scraping Browser API, `country-us` | its own | refused twice, then **served** when retried later that day, and on every fetch after that in the session: 120 products a page |

So: use `--cdp-endpoint` with a `country-us` Scraping Browser endpoint. A
different residential proxy is worth a try, not a guarantee; what changed the
answer in testing was the client and the exit together. Note the deny can
arrive under HTTP 200 — this repo recognises it by its own markers, not the
status.

**`akamai-challenge`** — Akamai's behavioural challenge ("Powered and
protected by Akamai", a press-and-hold button). This repo does not implement
a solver for it. Over `--cdp-endpoint` the Scraping Browser's
`Captcha.setAutoSolve` is enabled (logged as "enabled" by Playwright and
pyppeteer in live runs); whether it clears this particular challenge has not
been observed, because the challenge was not met over the Scraping Browser.

**A refusal from the Scraping Browser too.** The same `country-us` endpoint
was refused twice and served when retried later the same day (table above). Wait, then
retry; or use a fresh profile (`pid`).

## Exit 4: no products

**A category URL without its `?CN=…` filter.** The filter is what selects the
category; the August prototype reported that `/catalog/womens-clothing.jsp`
alone redirects to the homepage (not re-measured here). Copy the URL from the
browser's address bar after clicking into the category; it carries the
filter, e.g. `?CN=Gender:Womens+Department:Clothing`.

**A search that matched nothing.** A correct answer. `--allow-empty` writes
the empty files if your pipeline wants them.

## Exit 5: never obtained

**`could not connect to --cdp-endpoint … 500`** — a Scraping Browser profile
allows ONE live connection at a time, and the previous run's may not have
been released yet. Measured: a run started seconds after another got 500,
and the same command a minute later ran. Wait, or use another `pid`.

**`The proxy exit is unusable (ERR_PROXY_CONNECTION_FAILED)`** — the browser
could not reach the proxy at all. Check the HOST first: the 2Captcha proxy
gateway is the host shown in your proxy dashboard (`eu.proxy.2captcha.com`
answered on port 2334 on 2026-09-25), and the exit COUNTRY is a segment of
the login (`-region-…` / `-country-…`), not a different host. A made-up
`us.proxy.2captcha.com` resolves to the 2captcha.com website and never
answers on 2334.

**`came back as an empty document every time`** — the page never arrived.
Selenium does this through a proxy it cannot authenticate (it strips the
credentials and warns): the gateway returns nothing and Chromium shows a
blank page. Use Playwright or pyppeteer for an authenticated proxy.

**`Fingerprint API returned 403: … ERROR_FINGERPRINT_PLAN_REQUIRED`** — the
key is fine; the Fingerprint API is a separate subscription.

## Exit 6: partial

A later page was refused, or the run was cut short. The rows written are
real; `<out>.meta.json` names the pages that failed (`pages_failed`) and, for
a concurrent run whose workers died, the pages never attempted
(`pages_unattempted`) — by the listing's own page number, so a run started on
page 3 names page 4, not "page 2". `diff_runs.py` refuses to
compare a partial run, because its unfetched pages would read as delisted
products.

## Usage errors

**Selenium and `--cdp-endpoint`** — Selenium cannot send the endpoint's
password (chromedriver's `debuggerAddress` is a bare `host:port`) and says so
with exit 2. If `KOHLS_CDP_ENDPOINT` is in your `.env` and you want Selenium
to launch its own Chrome, run `KOHLS_CDP_ENDPOINT= python3 selenium_scraper.py …`.

**`--mode product` needs a `/product/prd-NNN/` URL**, and `--mode listing`
refuses one. A homepage or an account page is neither.

## Crashes

**pyppeteer: `could not start its browser (BrowserError: Browser closed
unexpectedly)`** — pyppeteer 2.0's own Chromium is old, and on an
Apple-silicon Mac it is an x86_64 build that does not start. Pass
`--chromium-path "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"`
(or Playwright's Chrome for Testing).

**`Page N hit an INTERNAL error (…)`, exit 1** — a bug in this scraper, not
the site. The pages gathered before it are still written (partial, stop
reason `internal_error`); the log carries a traceback with credentials
already masked. Please file it.

Anything else with a traceback: file a bug with the command (credentials
replaced by `***`) and the whole output.

## Things that look like bugs and are not

- **Two runs of the same category return different products.** Kohl's
  default "Featured" ordering moves: a page 1 re-fetched 24 seconds later
  kept 115 of its 120 products, and a page 1 fetched about two minutes before
  page 2 shared 30 ids with it. The `sort` column says which ordering a row came from; for a
  stable sample use a sorted URL (`&S=4`, Price Low-High, returned an
  identical page 1 twice).
- **`Dropped N row(s) already seen on an earlier page`** — the same drift,
  seen across pages of one run.
- **`brand` is empty on every listing row.** The listing payload has no brand
  field. `--mode product` fills it.
- **`in_stock` is always true on listings.** Measured on 863 of 863 listing
  rows; the listing appears to show only items that can ship. `--mode
  product` counts per-SKU availability, where both values occur.
- **`rating` and `review_count` are both empty on a row.** The product has no
  reviews; the site writes that as 0/0, and zero is not a rating.
- **`price_max` is set.** The product sells at a range of prices across sizes
  or colours; `price` is the low end.
