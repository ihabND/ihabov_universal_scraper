"""Errors a scrape can end with, each with the HTTP status the API answers with."""


class ScrapeError(Exception):
    status_code = 502


class InvalidURLError(ScrapeError):
    status_code = 400


class BrowserDisabledError(ScrapeError):
    status_code = 400


class BlockedURLError(ScrapeError):
    status_code = 403


class ContentTooLargeError(ScrapeError):
    status_code = 413


class UnsupportedContentError(ScrapeError):
    status_code = 415


class FetchError(ScrapeError):
    pass


class ParseError(ScrapeError):
    pass


class BlockedError(ScrapeError):
    pass


class BrowserBusyError(ScrapeError):
    status_code = 503
