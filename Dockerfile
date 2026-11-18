FROM python:3.12.14 AS builder
WORKDIR /app
RUN python -m venv .venv
COPY requirements.txt ./
RUN .venv/bin/pip install -r requirements.txt

FROM python:3.12.14-slim
WORKDIR /app
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1
COPY --from=builder /app/.venv .venv/
# Chromium and the Linux libraries it needs, before `COPY . .` so code changes don't download it again.
RUN .venv/bin/playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home scraper
COPY . .
# The browser loads untrusted pages, so nothing runs as root.
USER scraper
CMD ["/app/.venv/bin/uvicorn", "scraper.app:app", "--host", "0.0.0.0", "--port", "8080"]
