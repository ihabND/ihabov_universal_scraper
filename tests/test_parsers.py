from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from scraper.errors import ParseError
from scraper.page import Page
from scraper.parsers import PARSERS, eu_number, find_parser, parse_page

QUOTES_PAGE = b"""<html><body>
<div class="quote"><span class="text">\xe2\x80\x9cSimple is better.\xe2\x80\x9d</span>
  <small class="author">Tim Peters</small><a class="tag">python</a><a class="tag">zen</a></div>
</body></html>"""

# JSON-LD as many shops publish it: a list holding an @graph, with the product inside.
BOOK_PAGE = b"""<html><head><script type="application/ld+json">
[{"@graph": [{"@type": "WebSite", "name": "Shop"},
             {"@type": "Product", "name": "Tvr\xc4\x91ava", "isbn": "9788610023732", "sku": "25486",
              "brand": {"name": "Vulkan"}, "offers": {"price": "799.20", "priceCurrency": "RSD"}}]}]
</script></head><body><h1>Tvr\xc4\x91ava</h1></body></html>"""


@pytest.mark.parametrize("parser", PARSERS, ids=lambda p: p.host)
def test_each_example_url_is_handled_by_its_parser(parser):
    assert find_parser(parser.example) is parser


def test_unsupported_sites_have_no_parser():
    assert find_parser("https://example.com/") is None


def test_parses_a_listing_page():
    parser = find_parser("https://quotes.toscrape.com/page/2/")
    assert parse_page(parser, Page("https://quotes.toscrape.com/page/2/", 200, QUOTES_PAGE)) == [
        {"quote": "Simple is better.", "author": "Tim Peters", "tags": ["python", "zen"]}
    ]


IPON_URL = "https://iponcomp.com/shop/product/valueline-usb-32-gen-1-hub-4-ports-type-c-connection-cable/1871163"
FIXTURES = Path(__file__).parent / "fixtures"
VULKAN_IN_STOCK = "https://www.knjizare-vulkan.rs/domaci-klasici/316418-dervis-i-smrt"
VULKAN_OUT_OF_STOCK = "https://www.knjizare-vulkan.rs/domaci-klasici/25486-tvrdava"


def test_parses_json_ld():
    parsed = parse_page(find_parser(IPON_URL), Page(IPON_URL, 200, BOOK_PAGE))
    assert parsed["name"] == "Tvrđava"
    assert (parsed["price"], parsed["currency"], parsed["brand"]) == (799.2, "RSD", "Vulkan")


def test_json_ld_only_parsing_gives_the_same_result():
    parser = find_parser(IPON_URL)
    assert parser.json_ld_only
    assert parse_page(parser, Page(IPON_URL, 200, BOOK_PAGE)) == parser.parse(
        BeautifulSoup(BOOK_PAGE, "lxml"), IPON_URL
    )


def test_vulkan_book_in_stock_is_read_from_the_page_html():
    # Saved from the live site in November 2026, after Vulkan removed its JSON-LD product data.
    page = Page(VULKAN_IN_STOCK, 200, (FIXTURES / "vulkan_in_stock.html").read_bytes())
    parsed = parse_page(find_parser(VULKAN_IN_STOCK), page)
    assert parsed["name"] == "DERVIŠ I SMRT"
    assert (parsed["author"], parsed["publisher"]) == ("Meša Selimović", "VULKAN IZDAVAŠTVO")
    # The book's own price block, never a price from the mini cart or a recommendation.
    assert (parsed["price"], parsed["regular_price"], parsed["currency"]) == (1444.15, 1699.01, "RSD")
    assert parsed["available"] is True
    assert (parsed["isbn"], parsed["product_id"]) == ("9788610063172", "316418")
    assert parsed["sku"] and parsed["description"]


def test_vulkan_book_out_of_stock_has_no_price():
    page = Page(VULKAN_OUT_OF_STOCK, 200, (FIXTURES / "vulkan_out_of_stock.html").read_bytes())
    parsed = parse_page(find_parser(VULKAN_OUT_OF_STOCK), page)
    assert (parsed["name"], parsed["author"], parsed["isbn"]) == ("TVRĐAVA", "Meša Selimović", "9788610009736")
    assert (parsed["price"], parsed["regular_price"], parsed["available"]) == (None, None, False)


@pytest.mark.parametrize(
    ("text", "number"),
    [("1.444,15", 1444.15), ("254,86", 254.86), ("9.900,00 RSD", 9900.0), ("1.699", 1699.0), ("", None)],
)
def test_serbian_number_format(text, number):
    assert eu_number(text) == number


def test_a_page_without_the_data_is_a_parse_error():
    url = "https://www.knjizare-vulkan.rs/domaci-klasici/25486-tvrdava"
    with pytest.raises(ParseError):
        parse_page(find_parser(url), Page(url, 200, b"<html><body>Not found</body></html>"))
