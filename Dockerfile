FROM python:3.12.14 AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app


RUN python -m venv .venv
COPY requirements.txt ./
RUN .venv/bin/pip install -r requirements.txt
FROM python:3.12.14-slim
WORKDIR /app
# Chromium goes in a fixed folder that Playwright also looks in at runtime.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1
COPY --from=builder /app/.venv .venv/
# Downloads Chromium plus the Linux libraries it needs. This runs before `COPY . .`
# so code changes don't re-download the browser on every deploy.
RUN .venv/bin/playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
COPY . .
CMD ["/app/.venv/bin/uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
