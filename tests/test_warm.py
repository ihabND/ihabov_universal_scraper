"""Browsers are started in the background at boot, so the first browser request doesn't wait for one."""

import threading

from fastapi.testclient import TestClient

from scraper import app as app_module
from scraper import browser
from scraper.errors import FetchError


def test_each_browser_worker_starts_its_own_browser(monkeypatch):
    threads = []
    monkeypatch.setattr(browser, "get_browser", lambda: threads.append(threading.current_thread().name))
    assert browser.warm_browsers() == browser.BROWSER_WORKERS
    assert len(set(threads)) == browser.BROWSER_WORKERS


def test_a_browser_that_cannot_start_does_not_stop_the_server(monkeypatch):
    def broken():
        raise FetchError("The browser could not start: executable doesn't exist")

    monkeypatch.setattr(browser, "get_browser", broken)
    assert browser.warm_browsers() == 0


def test_warming_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(browser, "BROWSER_WARM", False)
    assert browser.start_warming() is None


def test_the_app_starts_warming_on_startup(monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "start_warming", lambda: calls.append(1))
    with TestClient(app_module.app):
        pass
    assert calls == [1]
