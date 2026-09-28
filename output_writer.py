"""
output_writer.py
-----------------
Shared row model + JSON/CSV writers used by all three scrapers.

Two modes, one row shape
------------------------
    --mode listing   category grids (/catalog/...jsp) and search results
                     (/search.jsp?search=...)              -> Product
    --mode product   one /product/prd-NNN/... page         -> Product, with the
                     trailing detail-only fields populated

Both modes yield the SAME class, because on kohls.com a detail page is not a
different kind of object from a tile — it is the same product described more
fully, under the same id (`webID`, the number in `prd-NNN`). So there is no
second dataclass here, and a listing row and a product row join on `sku`.
`diff_runs.py` still refuses to diff a listing run against a product run:
the detail-only columns are null on one side and filled on the other, so a
diff would describe the mode change rather than the catalogue.

`Product` keeps the family's field order exactly, with the Kohl's-specific
columns appended after `price_source`, so a consumer written against another
repo in this family still reads the first sixteen columns unchanged.

Everything below is row-class-agnostic: pass `row_cls` so an empty CSV still
gets the right header for the mode that produced it.
"""

import csv
import json
import os
import tempfile
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Callable, Optional, List, Set, Sequence, Any, Tuple, Type


# One shop, one host. Kept as a constant rather than written into every row
# builder so `source` means the same thing in every repo of this family: the
# hostname the row was read from.
SOURCE_DEFAULT = "kohls.com"


@dataclass
class Product:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""
    # Kohl's `webID` — the number in every product URL (/product/prd-3500577/…)
    # and the id the listing payload and the detail page both carry. Verified
    # to be the same value on both routes, so a listing run and a product run
    # join on it.
    sku: Optional[str] = None
    title: Optional[str] = None
    # Null on every LISTING row, and that is measured rather than missed: the
    # listing payload carries no brand field at all (863 rows across eight
    # live pages, 2026-09-25). The brand is often the leading words of the
    # title ("Women's Croft & Barrow® Essential Crewneck Tee" — which also
    # shows why splitting the title does not work: the brand is not the
    # first token). It IS populated by --mode product, from the detail page's
    # own `brand` field.
    brand: Optional[str] = None
    # The lowest price the customer can pay now: the sale price where one is
    # running, the regular price otherwise. On a product sold in several
    # sizes or colours at different prices this is the LOW end of the range;
    # `price_max` carries the top.
    price: Optional[float] = None
    # From the page's own schema.org `offers.priceCurrency` — never a
    # compiled-in "USD". A row whose page stated no currency says None.
    currency: Optional[str] = None
    # Kohl's regular ("Reg.") price, present only when a sale price is
    # running below it. Low end of the range, like `price`.
    original_price: Optional[float] = None
    # Computed from `price` and `original_price` — the LOW ends of both
    # ranges — never read from a printed badge. On a range product this is
    # the discount at the cheapest variant, not a promise for every variant.
    discount_pct: Optional[float] = None
    # Null together with `review_count` when the product has no reviews: the
    # payload writes an unreviewed product as `avgRating: 0, count: 0`
    # (158 of 863 measured rows), and zero is not a rating.
    rating: Optional[float] = None
    review_count: Optional[int] = None
    # Listing: the payload's own `isAvailableforShip`. Measured TRUE on 863 of
    # 863 rows, so a False on a listing row has never been observed and is
    # less proven than a True — the listing appears to show only items that
    # can ship. --mode product reads per-SKU availability instead, where both
    # values are real (95 of 286 SKUs in stock on one measured page).
    in_stock: Optional[bool] = None
    image_url: Optional[str] = None
    category: Optional[str] = None
    # Where `price` came from:
    #   "catalog"  — the listing's own server-rendered product payload
    #                (the Astro island's `catalogData.products`). Measured to
    #                agree with the rendered tile on 323 of 323 tiles.
    #   "product"  — a detail page's own product model (--mode product).
    #   "dom"      — the URL-pattern fallback ran; the price was read out of
    #                the tile's text.
    # diff_runs.py reports a price change that comes with a price_source
    # change as `source_changed`, not `changed`.
    price_source: Optional[str] = None

    # ---- Kohl's-specific, appended so the family prefix above is stable ----
    # Which listing page this row came from (1-based) and its position among
    # the rows this run EMITTED from that page. Position counts emitted rows,
    # not payload slots: the payload interleaves 0-12 sponsored entries per
    # page (varying between fetches of the same URL), and numbering by slot
    # would shift every position whenever that count changed.
    page: Optional[int] = None
    position: Optional[int] = None
    # The top of the price range, or None when the product has one price.
    # 128 of 863 measured rows have a `price_max` and 25 an
    # `original_price_max`, so a single `price` column would silently drop the
    # top half of a real fact about one row in seven.
    price_max: Optional[float] = None
    original_price_max: Optional[float] = None
    # Kohl's own label for what kind of price is running: "sale",
    # "clearance", "sale+clearance", "clearance+regular" (some variants on
    # clearance, the rest at the regular price), … or None for a plain
    # regular price. Read verbatim from the payload's `priceLabel`.
    price_label: Optional[str] = None
    # The listing's ordering, as the SITE reports it ("Featured",
    # "Price Low-High", …). A column rather than a sidecar field because it
    # decides WHICH rows a run of N pages holds, not just their order: the
    # default "Featured" ordering was measured drifting within minutes (a
    # page 1 re-fetched 24 seconds later kept 115 of its 120 ids, and a page
    # 1 fetched about two minutes before page 2 shared 30 ids with it), while
    # "Price Low-High" returned an identical page 1 twice. diff_runs.py refuses to compare
    # runs taken under different orderings.
    sort: Optional[str] = None
    # ---- populated by --mode product only; null on a listing run ----
    # How many SKUs (size x colour combinations) the product has, and how many
    # of them the page reports "In Stock".
    skus_total: Optional[int] = None
    skus_in_stock: Optional[int] = None


# Row classes by --mode, so an engine maps its mode to a schema in one place.
ROW_CLASS_BY_MODE = {"listing": Product, "product": Product}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku` and
# to hand to diff_runs.py. Both of this repo's modes qualify.
UNIQUE_BY_SKU_MODES = ("listing", "product")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output.

    On kohls.com this is NOT only a guard against a re-fetch. The default
    "Featured" ordering drifts while a run is in progress: a page 1 fetched
    about two minutes before page 2 shared 30 of its 120 ids with it
    (measured 2026-09-25), so a Featured run can see the same product on two
    pages. Dedupe keeps the first; the engines log how many they
    dropped, because a large number means the ordering moved under the run.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.

    Both of this repo's modes are one row per `sku`, so `key` is never
    overridden here — the parameter exists because the rest of the family
    shares this function and one of them needs it.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


# ---------------------------------------------------------------------------
# Atomic publication
# ---------------------------------------------------------------------------
# Every output file is written to a temporary file in the SAME directory,
# fsync'd, and only then renamed over the real path with os.replace, which
# is atomic on one filesystem. A run killed half-way through a write, a full
# disk, or an exception inside the CSV writer therefore leaves the previous
# run's file exactly as it was, instead of a truncated file under the real
# name — which is what "an empty result never overwrites a good one" (see
# `save`) has to mean for a failure DURING the write, too.
#
# A run's files (JSON, CSV, sidecar) are all staged first and renamed
# together at the end, sidecar last, so a failure while writing any one of
# them publishes none of them.
#
# Read once at import: os.umask can only be read by setting it, which is not
# thread-safe, and mkstemp creates files 0600 — the published file should get
# the same mode a plain open() would have given it.
_UMASK = os.umask(0)
os.umask(_UMASK)


def _stage(path: str, write: Callable[[Any], None], newline: Optional[str] = None) -> str:
    """Write via `write(fileobj)` to a temp file beside `path`; return its path."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory,
                               prefix="." + os.path.basename(path) + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as f:
            write(f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o666 & ~_UMASK)
    except BaseException:
        _discard([(tmp, path)])
        raise
    return tmp


def _commit(staged: Sequence[Tuple[str, str]]) -> None:
    """Rename every staged (tmp, final) pair into place, in order."""
    for tmp, final in staged:
        os.replace(tmp, final)


def _discard(staged: Sequence[Tuple[str, str]]) -> None:
    for tmp, _ in staged:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _json_writer(rows: Sequence[Any]) -> Callable[[Any], None]:
    return lambda f: json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def _csv_writer(rows: Sequence[Any], row_cls: Type) -> Callable[[Any], None]:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]

    def write(f):
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})
    return write


def write_json(rows: Sequence[Any], path: str) -> None:
    _commit([(_stage(path, _json_writer(rows)), path)])


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    _commit([(_stage(path, _csv_writer(rows, row_cls), newline=""), path)])


# ---------------------------------------------------------------------------
# Raw page material: debug dumps and --dump-html
# ---------------------------------------------------------------------------
def write_private_text(path: str, text: str) -> None:
    """Write `text` to `path` readable by the owner only (0600).

    For raw page material: SECURITY.md warns that a served page can carry
    session material, and these dumps are the one place it reaches disk
    unmasked. They were written with a plain open(), so they inherited the
    umask (usually world-readable 0644). fchmod as well as the create mode,
    because O_CREAT's mode does nothing to a file that already exists.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            f.write(text or "")
    finally:
        if fd != -1:
            os.close(fd)


def make_private(path: str) -> None:
    """chmod 0600 a file some other code wrote (a driver's screenshot)."""
    if os.path.exists(path):
        os.chmod(path, 0o600)


def output_dir_problem(out_prefix: str) -> Optional[str]:
    """Why `--out` cannot be written, or None when it can.

    Checked BEFORE a browser starts. Found by an audit on 2026-09-27: with
    `--out` in a directory that does not exist, a refused page became
    "exit 5, page_error" instead of "exit 3, blocked" (writing its debug
    dump raised), and a run that DID gather rows crashed with a traceback
    at the final write — after every page had been fetched and paid for.
    """
    directory = os.path.dirname(os.path.abspath(out_prefix))
    if not os.path.isdir(directory):
        return f"the --out directory {directory} does not exist"
    if not os.access(directory, os.W_OK | os.X_OK):
        return f"the --out directory {directory} is not writable"
    return None


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On kohls.com this is Akamai: an "Access Denied" page (under HTTP 403, and
# measured under HTTP 200 too) or its behavioural challenge. It does NOT
# cover Kohl's own "page not available" page, which is what a page number
# past the end of a listing redirects to — that is a served answer.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
EXIT_PARTIAL = 6


# Exit code for a run that never GOT its pages: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Distinct from EXIT_NO_PRODUCTS because those are opposite facts. Exit 4 is
# a statement about the CATALOGUE — "we asked, and the answer was nothing" —
# so handing it to a run that never reached the site tells a pipeline the
# listing is empty when nothing was read at all.
#
# 5 rather than a new number, and 5 rather than EXIT_PARTIAL:
#
#   * this family's contract already reserves 5 for a transport failure
#     (scraper_api_client has used it for a remote API error since it was
#     written), so this needs no new code and no per-repo table for a caller
#     driving more than one of these scrapers;
#   * EXIT_PARTIAL (6) means "some rows were gathered and the output is
#     incomplete". A run holding nothing writes no output at all, so a
#     consumer that reads the file on a 6 finds either nothing or the
#     PREVIOUS run's good data, which `save` deliberately does not
#     overwrite. Exit 5 promises no file.
#
# Deliberately NOT applied when rows WERE gathered: a timeout on page 7 of
# 10 is a partial run (exit 6, output written), which is already right. This
# decides only what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = _meta_path(out_prefix)
    _commit([(_stage(path, _meta_writer(meta)), path)])
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def _meta_path(out_prefix: str) -> str:
    return f"{out_prefix}.meta.json"


def _meta_writer(meta: dict) -> Callable[[Any], None]:
    return lambda f: json.dump(meta, f, ensure_ascii=False, indent=2)


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "listing", source: str = SOURCE_DEFAULT,
             listing: Optional[dict] = None,
             pages_unattempted: Optional[List[int]] = None,
             outputs: Optional[List[str]] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` is recorded because it is not implied by the repo: the same output
    prefix can hold a listing run or a product run. diff_runs.py refuses a
    pair whose modes or sources differ.

    `listing` carries the SITE's own arithmetic about the listing, from page
    1 — `total_results`, `pages_available`, `per_page` — so "complete" is not
    read as "exhaustive". A 3-page run of a 1,317-page category is a complete
    run of what was asked and a 0.2% sample of the category; a sidecar that
    said only "complete" would be lying by omission.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.

    Page numbers are the listing's own (the `WS=` page), not the order this
    run fetched them in: a run started on page 3 whose second request fails
    names page 4. It named page 2 until an audit on 2026-09-27.

    `pages_unattempted` lists pages that were planned and never fetched —
    the queue a concurrent run's workers left behind when they died. They
    are not in `pages_failed` (nothing was tried), and before this field
    existed they were in no field at all, so a partial sidecar could not say
    which pages it was missing. Pages past the listing's end are not
    unattempted; there was nothing there to get.

    `outputs` names the data files THIS run wrote, by basename. With
    `--format json` a CSV left beside it by an earlier run is not this run's,
    and the sidecar is the only place that can say so.
    """
    return {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "pages_unattempted": pages_unattempted or [],
        "outputs": outputs or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "listing": listing or {},
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 products -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.

    Both files are staged and then renamed into place together (see
    "Atomic publication"): a failure while writing either leaves both
    previous files untouched.
    """
    rc, staged = _stage_rows(rows, out_prefix, fmt, allow_empty, row_cls)
    _commit(staged)
    _report_saved(rows, staged)
    return rc


def _stage_rows(rows: Sequence[Any], out_prefix: str, fmt: str,
                allow_empty: bool, row_cls: Type) -> Tuple[int, List[Tuple[str, str]]]:
    """Stage the row files `save` would write; return (exit code, staged pairs)."""
    if not rows and not allow_empty:
        print(f"[!] 0 products — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS, []

    staged: List[Tuple[str, str]] = []
    try:
        if fmt in ("json", "both"):
            path = f"{out_prefix}.json"
            staged.append((_stage(path, _json_writer(rows)), path))
        if fmt in ("csv", "both"):
            path = f"{out_prefix}.csv"
            staged.append((_stage(path, _csv_writer(rows, row_cls), newline=""), path))
    except BaseException:
        _discard(staged)
        raise
    return (0 if rows else EXIT_NO_PRODUCTS), staged


def _report_saved(rows: Sequence[Any], staged: Sequence[Tuple[str, str]]) -> None:
    for _, final in staged:
        print(f"[+] Saved {len(rows)} products -> {final}")


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "end_of_listing" is the strongest of these on kohls.com: the site states
# its own `totalPages` and which page it SERVED (`currentPage`), so the end
# is arithmetic — and a page past the end is a 404 redirect to Kohl's own
# "page not available" page, which is also recorded as the end. Leaving it
# out of this tuple would report exit 6 for a correct, complete run (a
# sibling repo did exactly that).
#
# "no_new_products" is the DATA-based terminator (a page contributed nothing
# not already seen). There is no "pagination_exhausted": Kohl's renders its
# pager as a <select> and a button with no href at all, so no selector could
# ever find a next link to be exhausted.
#
# "single_page_mode" is complete by construction: --mode product reads one
# page because one page is all there is.
COMPLETE_STOP_REASONS = ("completed", "end_of_listing", "no_new_products",
                         "single_page_mode")

# A page the site SERVED — the listing island was there, or product links
# were — that parsed to zero rows. That is this parser's fault, not the
# catalogue's (§20), so it has its own stop reason and is NOT complete:
# mid-run it makes the run partial (exit 6). With nothing gathered at all it
# keeps the "no products" exit (4) rather than 5, because the page was
# obtained; the stop reason is what names whose problem it is.
PARSER_STOP_REASONS = ("served_but_unparsed",)


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "listing", source: str = SOURCE_DEFAULT,
               listing: Optional[dict] = None,
               pages_unattempted: Optional[List[int]] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.

    Row files and sidecar are published in ONE commit, sidecar last: they
    are staged first, and nothing is renamed into place until all of them
    were written. A sidecar describing a run whose rows never landed (or
    the reverse) cannot be left behind by a failure mid-write.
    """
    # Completeness is decided by the reason AND by the evidence. A named
    # list of stop reasons cannot cover a failure recorded somewhere else,
    # and `pages_failed` is somewhere else: a run whose loop ended for a
    # COMPLETE reason while individual pages failed reported exit 0 and
    # `status: complete` with a non-empty `pages_failed` in the same
    # sidecar — a file that contradicts itself, and a pipeline branching
    # on `status` reading a short run as a whole one.
    #
    # Found by a third-party audit of a sibling repo and measured across
    # the family by CALLING each `finish_run` rather than grepping for the
    # fix: 28 of 32 repos behaved this way. Same shape as the exit-code
    # unification this file already carries — a rule keyed on a list of
    # names has a hole for every name nobody added to it.
    complete = (stop_reason in COMPLETE_STOP_REASONS and not pages_failed
                and not pages_unattempted)
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    rc, staged = _stage_rows(rows, out_prefix, fmt, allow_empty, row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        meta = run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, pages_unattempted=pages_unattempted,
            outputs=[os.path.basename(final) for _, final in staged],
            mode=mode, source=source, listing=listing,
            start_url=start_url, final_url=final_url, products=len(rows))
        meta_path = _meta_path(out_prefix)
        try:
            staged.append((_stage(meta_path, _meta_writer(meta)), meta_path))
        except BaseException:
            _discard(staged)
            raise
        _commit(staged)
        _report_saved(rows, staged[:-1])
        print(f"[+] Wrote run metadata -> {meta_path} (status={status})")

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are different facts and a pipeline branches on them
        # (blocked is not empty is not "never reached"):
        #
        #   blocked            something stood between the run and the content
        #   did not complete   we never got the pages — a dead proxy, a load
        #                      timeout, an edge serving something else
        #   completed          we asked, and the answer was nothing
        #
        # Keyed on `not complete` rather than on a list of stop reasons, on
        # purpose: a list cannot cover a reason nobody has added to it yet,
        # so a new one falls silently through to "the catalogue is empty" —
        # which is the defect this branch exists to prevent.
        if blocked:
            return EXIT_BLOCKED
        if stop_reason in PARSER_STOP_REASONS:
            print(f"[!] The page was served and parsed to 0 rows "
                  f"({stop_reason}) — exit {EXIT_NO_PRODUCTS}, and this is the "
                  f"PARSER's problem, not an empty catalogue. Re-run with "
                  f"--dump-html and report it.")
            return EXIT_NO_PRODUCTS
        if not complete:
            print(f"[!] Nothing was gathered and the run did not finish "
                  f"({stop_reason}) — exit {EXIT_FETCH_FAILED}, NOT an empty "
                  f"result (exit {EXIT_NO_PRODUCTS}). Nothing can be "
                  f"concluded about the catalogue from this run.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
