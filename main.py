"""Entry point, so the app can be started from the project root with `uvicorn main:app`."""

from scraper.app import app

__all__ = ["app"]
