# Builds the Playwright engine (the one the README recommends) into a
# container with its own Chromium — for a scheduled job, not required for
# local development (`pip install` directly is simpler there).
#
#   docker build -t kohls-scraper .
#   docker run --rm -v "$PWD/out:/out" --env-file .env kohls-scraper \
#     --url "https://www.kohls.com/catalog/cuisinart.jsp?CN=Brand:Cuisinart" \
#     --pages 3 --out /out/cuisinart
#
# Credentials come in at RUN time (--env-file, or -e), never at build time:
# a credentials file baked into an image is published to everyone who can
# pull it. Nothing here copies one, and .dockerignore excludes it too.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements-playwright.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-playwright.txt \
    # Playwright's own apt-get for Chromium's shared-library dependencies —
    # not pip packages, so this has to run as a separate, explicit step.
    && playwright install --with-deps chromium

# Every module playwright_scraper.py imports, transitively, plus diff_runs.py
# as a companion tool. smoke_test.py checks this list against the
# entrypoint's real import graph: every repo in this family once shipped an
# image that died with ModuleNotFoundError on every invocation, `--help`
# included, because one module was missing from a list like this.
COPY browser_bridge.py captcha_solver.py env_config.py fingerprint_client.py \
     output_writer.py page_flow.py playwright_scraper.py product_parser.py \
     proxy_pool.py diff_runs.py ./

ENTRYPOINT ["python3", "playwright_scraper.py"]
CMD ["--help"]
