import pytest
from bs4 import BeautifulSoup

from scraper.errors import ParseError
from scraper.page import Page
from scraper.parsers import PARSERS, find_parser, parse_page

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


def test_parses_json_ld():
    url = "https://www.knjizare-vulkan.rs/domaci-klasici/25486-tvrdava"
    parsed = parse_page(find_parser(url), Page(url, 200, BOOK_PAGE))
    assert parsed["name"] == "Tvrđava"
    assert (parsed["price"], parsed["currency"], parsed["publisher"]) == (799.2, "RSD", "Vulkan")


def test_json_ld_only_parsing_gives_the_same_result():
    url = "https://www.knjizare-vulkan.rs/domaci-klasici/25486-tvrdava"
    parser = find_parser(url)
    assert parser.json_ld_only
    assert parse_page(parser, Page(url, 200, BOOK_PAGE)) == parser.parse(BeautifulSoup(BOOK_PAGE, "lxml"), url)


def test_a_page_without_the_data_is_a_parse_error():
    url = "https://www.knjizare-vulkan.rs/domaci-klasici/25486-tvrdava"
    with pytest.raises(ParseError):
        parse_page(find_parser(url), Page(url, 200, b"<html><body>Not found</body></html>"))
