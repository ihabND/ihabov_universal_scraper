from . import jsonld, parser

parser(
    "iponcomp.com",
    r"/shop/product/[^/]+/\d+/?",
    "https://iponcomp.com/shop/product/valueline-usb-32-gen-1-hub-4-ports-type-c-connection-cable/1871163",
    label="Shop product page",
    pages="A product page, like /shop/product/<name>/1871163",
    fields="name, brand, price (null when the shop lists no price), currency, availability, shipping_price, "
    "category, sku, mpn, ean, description, image, url",
    json_ld_only=True,
)(jsonld.product)
