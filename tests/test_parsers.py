import json
from pathlib import Path

import pytest

from scraper.core.errors import ParseError
from scraper.core.page import Page
from scraper.parsers import GENERIC, PARSERS, find_parser, parse_page, ready_check, seed_tier
from scraper.parsers.text import eu_number

FIXTURES = Path(__file__).parent / "fixtures" / "parsers"
SNAPSHOTS = sorted(FIXTURES.glob("*.json"))


def load(snapshot: Path) -> tuple[str, Page, object]:
    expected = json.loads(snapshot.read_text(encoding="utf-8"))
    page = Page(expected["url"], 200, snapshot.with_suffix(".html").read_bytes())
    return expected["url"], page, expected["parsed"]


@pytest.mark.parametrize("snapshot", SNAPSHOTS, ids=lambda p: p.stem)
def test_saved_pages_parse_to_their_snapshot(snapshot):
    url, page, parsed = load(snapshot)
    assert parse_page(find_parser(url), page) == parsed


@pytest.mark.parametrize("snapshot", SNAPSHOTS, ids=lambda p: p.stem)
def test_documented_fields_match_the_output(snapshot):
    url, _, parsed = load(snapshot)
    keys = set(parsed[0] if isinstance(parsed, list) else parsed)
    assert {field.split(" (")[0].strip() for field in find_parser(url).fields.split(",")} == keys


def test_every_parser_has_a_saved_page():
    covered = {find_parser(load(snapshot)[0]) for snapshot in SNAPSHOTS}
    assert [p.name for p in PARSERS if p not in covered] == []


@pytest.mark.parametrize("parser", PARSERS, ids=lambda p: p.name)
def test_each_example_url_is_handled_by_its_parser(parser):
    assert find_parser(parser.example) is parser


def test_unsupported_pages_have_no_parser():
    assert find_parser("https://example.com/") is None
    assert find_parser("https://books.toscrape.com/catalogue/x/y/z") is None


def test_json_ld_variants_still_parse():
    url = "https://iponcomp.com/shop/product/hub/1871163"
    ld = {"@type": "Product", "name": "Hub", "brand": "Valueline", "offers": [{"price": "10", "priceCurrency": "EUR"}]}
    page = Page(url, 200, f'<script type="application/ld+json">{json.dumps(ld)}</script>'.encode())
    parsed = parse_page(find_parser(url), page)
    assert (parsed["brand"], parsed["price"], parsed["currency"]) == ("Valueline", 10.0, "EUR")


def test_a_page_without_the_data_is_a_parse_error():
    url = "https://www.knjizare-vulkan.rs/domaci-klasici/25486-tvrdava"
    with pytest.raises(ParseError):
        parse_page(find_parser(url), Page(url, 200, b"<html><body>Not found</body></html>"))


def test_ready_is_true_once_the_data_is_on_the_page():
    url, page, _ = load(FIXTURES / "quotes.json")
    ready = ready_check(find_parser(url), url)
    assert ready(page.content) and not ready(b"<html></html>")


def test_imdb_starts_in_the_browser():
    assert seed_tier("imdb.com") == "browser" and seed_tier("books.toscrape.com") == "http"


@pytest.mark.parametrize(
    ("text", "number"),
    [("1.444,15", 1444.15), ("254,86", 254.86), ("9.900,00 RSD", 9900.0), ("1.699", 1699.0), ("", None)],
)
def test_serbian_number_format(text, number):
    assert eu_number(text) == number


# ---- Any other page: the structured data it carries -----------------------------------------

NEXT_PAGE = (
    b'<html><head><title>Laptops</title></head><body><div id="__next"></div>'
    b'<script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {"items": [{"name": "X1"}]}}}</script>'
    b'<script>var ytInitialData = {"contents": {"title": "NASA"}};</script>'
    b'<script>window.__INITIAL_STATE__ = {"tag": "photography"}; window.other = 1;</script>'
    b"</body></html>"
)


def test_any_other_page_gives_its_embedded_data():
    parsed = parse_page(GENERIC, Page("https://www.target.com/s?searchTerm=laptop", 200, NEXT_PAGE))
    assert parsed == {
        "title": "Laptops",
        "next_data": {"props": {"pageProps": {"items": [{"name": "X1"}]}}},
        "yt_initial_data": {"contents": {"title": "NASA"}},
        "initial_state": {"tag": "photography"},
    }


def test_any_other_page_gives_its_json_ld():
    url, page, _ = load(FIXTURES / "movie.json")
    parsed = parse_page(GENERIC, Page("https://example.com/film", 200, page.content))
    assert parsed["json_ld"][0]["name"] == "The Shawshank Redemption"


def test_a_page_without_structured_data_is_a_parse_error():
    with pytest.raises(ParseError):
        parse_page(GENERIC, Page("https://example.com/", 200, b"<html><title>Example</title><p>Hi</p></html>"))
