"""Offline unit test suite for ecommerce-jsonld-price-extractor-cli-lite."""

from __future__ import annotations

import gzip
import io
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cli  # noqa: E402
from extractor import (  # noqa: E402
    extract_from_html,
    extract_from_target,
    extract_from_url,
    normalize_availability,
    parse_price_and_currency,
)


class _MockHTTPResponse:
    def __init__(self, body: bytes, headers: dict, final_url: str) -> None:
        self._body = body
        self.headers = headers
        self._final_url = final_url

    def read(self) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._final_url

    def __enter__(self) -> "_MockHTTPResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class TestExtractor(unittest.TestCase):
    def test_01_jsonld_basic_product_and_offer(self) -> None:
        html = """
        <!doctype html>
        <html>
          <head>
            <title>Fallback Title</title>
            <script type="application/ld+json">
            {
              "@context": "https://schema.org/",
              "@type": "Product",
              "name": "Mechanical Keyboard 75%",
              "url": "https://shop.example.com/kb-75",
              "offers": {
                "@type": "Offer",
                "priceCurrency": "USD",
                "price": "129.99",
                "availability": "https://schema.org/InStock"
              }
            }
            </script>
          </head>
          <body><h1>Mechanical Keyboard</h1></body>
        </html>
        """
        res = extract_from_html(html, source="kb.html")
        self.assertEqual(res.extractor_source, "json-ld")
        self.assertEqual(res.name, "Mechanical Keyboard 75%")
        self.assertEqual(res.currency, "USD")
        self.assertEqual(res.price, Decimal("129.99"))
        self.assertEqual(res.availability, "in_stock")
        self.assertTrue(res.in_stock)
        self.assertEqual(res.url, "https://shop.example.com/kb-75")

    def test_02_jsonld_graph_and_id_reference(self) -> None:
        html = """
        <html><head>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@graph": [
              {"@type": "Organization", "@id": "#org", "name": "Example Shop"},
              {
                "@type": "Product",
                "@id": "#product",
                "name": "Studio Condenser Mic",
                "offers": {"@id": "#offer-1"}
              },
              {
                "@type": "Offer",
                "@id": "#offer-1",
                "price": 249.50,
                "priceCurrency": "EUR",
                "availability": "http://schema.org/LimitedAvailability",
                "url": "https://shop.example.com/mic"
              }
            ]
          }
          </script>
        </head></html>
        """
        res = extract_from_html(html)
        self.assertEqual(res.extractor_source, "json-ld")
        self.assertEqual(res.name, "Studio Condenser Mic")
        self.assertEqual(res.currency, "EUR")
        self.assertEqual(res.price, Decimal("249.5"))
        self.assertEqual(res.availability, "in_stock")
        self.assertTrue(res.in_stock)
        self.assertEqual(res.url, "https://shop.example.com/mic")

    def test_03_jsonld_aggregate_offer_low_price(self) -> None:
        html = """
        <html><head>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": ["Product", "IndividualProduct"],
            "name": "Ergonomic Mesh Chair",
            "offers": {
              "@type": "AggregateOffer",
              "lowPrice": "349.00",
              "highPrice": "499.00",
              "priceCurrency": "GBP",
              "availability": "https://schema.org/InStock"
            }
          }
          </script>
        </head></html>
        """
        res = extract_from_html(html)
        self.assertEqual(res.extractor_source, "json-ld")
        self.assertEqual(res.name, "Ergonomic Mesh Chair")
        self.assertEqual(res.currency, "GBP")
        self.assertEqual(res.price, Decimal("349.00"))
        self.assertEqual(res.price_str(), "349.00")
        self.assertTrue(res.in_stock)

    def test_04_jsonld_multiple_offers_prefers_instock(self) -> None:
        html = """
        <html><head>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "Wireless Mouse Pro",
            "offers": [
              {
                "@type": "Offer",
                "price": "79.00",
                "priceCurrency": "USD",
                "availability": "https://schema.org/OutOfStock"
              },
              {
                "@type": "Offer",
                "price": "84.95",
                "priceCurrency": "USD",
                "availability": "https://schema.org/InStock"
              }
            ]
          }
          </script>
        </head></html>
        """
        res = extract_from_html(html)
        self.assertEqual(res.extractor_source, "json-ld")
        self.assertEqual(res.price, Decimal("84.95"))
        self.assertEqual(res.availability, "in_stock")
        self.assertTrue(res.in_stock)

    def test_05_jsonld_price_specification_and_cdata_wrapper(self) -> None:
        html = """
        <html><head>
          <script type="application/ld+json; charset=utf-8">
          /* <![CDATA[ */
          {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "4K USB-C Monitor",
            "offers": {
              "@type": "Offer",
              "availability": "https://schema.org/OutOfStock",
              "priceSpecification": {
                "@type": "UnitPriceSpecification",
                "price": "499.90",
                "priceCurrency": "USD"
              }
            }
          }
          /* ]]> */
          </script>
        </head></html>
        """
        res = extract_from_html(html)
        self.assertEqual(res.extractor_source, "json-ld")
        self.assertEqual(res.name, "4K USB-C Monitor")
        self.assertEqual(res.price, Decimal("499.90"))
        self.assertEqual(res.currency, "USD")
        self.assertEqual(res.availability, "out_of_stock")
        self.assertFalse(res.in_stock)

    def test_06_opengraph_product_price_tags(self) -> None:
        html = """
        <html>
          <head>
            <meta property="og:title" content="Noise Cancelling Headphones" />
            <meta property="og:url" content="https://store.example.com/headphones" />
            <meta property="product:price:amount" content="199.95" />
            <meta property="product:price:currency" content="USD" />
            <meta property="product:availability" content="in stock" />
          </head>
          <body></body>
        </html>
        """
        res = extract_from_html(html)
        self.assertEqual(res.extractor_source, "opengraph")
        self.assertEqual(res.name, "Noise Cancelling Headphones")
        self.assertEqual(res.currency, "USD")
        self.assertEqual(res.price, Decimal("199.95"))
        self.assertEqual(res.availability, "in_stock")
        self.assertTrue(res.in_stock)
        self.assertEqual(res.url, "https://store.example.com/headphones")

    def test_07_opengraph_og_price_tags_oos(self) -> None:
        html = """
        <html>
          <head>
            <title>DAC Amplifier</title>
            <meta property="og:price:amount" content="1,250.00" />
            <meta property="og:price:currency" content="CAD" />
            <meta property="og:availability" content="oos" />
          </head>
          <body><h1>Desktop DAC Amplifier</h1></body>
        </html>
        """
        res = extract_from_html(html)
        self.assertEqual(res.extractor_source, "opengraph")
        self.assertEqual(res.name, "Desktop DAC Amplifier")
        self.assertEqual(res.currency, "CAD")
        self.assertEqual(res.price, Decimal("1250.00"))
        self.assertEqual(res.availability, "out_of_stock")
        self.assertFalse(res.in_stock)

    def test_08_microdata_nested_product_and_offer_with_void_tags(self) -> None:
        html = """
        <html>
          <body>
            <div itemscope itemtype="https://schema.org/Product">
              <h1 itemprop="name">Standing Desk Frame</h1>
              <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
                <meta itemprop="priceCurrency" content="EUR">
                <link itemprop="availability" href="https://schema.org/InStock">
                <span itemprop="price" content="389.00">€389,00</span>
              </div>
            </div>
          </body>
        </html>
        """
        res = extract_from_html(html)
        self.assertEqual(res.extractor_source, "microdata")
        self.assertEqual(res.name, "Standing Desk Frame")
        self.assertEqual(res.currency, "EUR")
        self.assertEqual(res.price, Decimal("389.00"))
        self.assertEqual(res.availability, "in_stock")
        self.assertTrue(res.in_stock)

    def test_09_microdata_flat_and_preorder(self) -> None:
        html = """
        <html>
          <body>
            <section itemscope itemtype="http://schema.org/Product">
              <span itemprop="name">Handheld Retro Console</span>
              <span itemprop="price">$179.99</span>
              <meta itemprop="availability" content="https://schema.org/PreOrder">
            </section>
          </body>
        </html>
        """
        res = extract_from_html(html)
        self.assertEqual(res.extractor_source, "microdata")
        self.assertEqual(res.name, "Handheld Retro Console")
        self.assertEqual(res.currency, "USD")
        self.assertEqual(res.price, Decimal("179.99"))
        self.assertEqual(res.availability, "preorder")
        self.assertFalse(res.in_stock)

    def test_10_regex_fallback_skips_strikethrough_and_scripts(self) -> None:
        html = """
        <html>
          <head>
            <title>NVMe SSD 2TB - ExampleStore</title>
            <script>var decoyPrice = "$9999.00";</script>
            <style>.price::before { content: "$8888.00"; }</style>
          </head>
          <body>
            <h1>NVMe SSD 2TB</h1>
            <div class="price-box">
              <span class="old-price"><del>$219.99</del></span>
              <span class="current-price">$149.95</span>
            </div>
            <p>Status: In stock and ready to ship.</p>
          </body>
        </html>
        """
        res = extract_from_html(html)
        self.assertEqual(res.extractor_source, "regex-fallback")
        self.assertEqual(res.name, "NVMe SSD 2TB")
        self.assertEqual(res.currency, "USD")
        self.assertEqual(res.price, Decimal("149.95"))
        self.assertEqual(res.availability, "in_stock")
        self.assertTrue(res.in_stock)

    def test_11_regex_fallback_euro_and_rub_formats(self) -> None:
        html_eur = """
        <html><body>
          <h1>Coffee Grinder Pro</h1>
          <div class="product-price">1.299,50 €</div>
          <p>Currently unavailable</p>
        </body></html>
        """
        res_eur = extract_from_html(html_eur)
        self.assertEqual(res_eur.extractor_source, "regex-fallback")
        self.assertEqual(res_eur.currency, "EUR")
        self.assertEqual(res_eur.price, Decimal("1299.50"))
        self.assertEqual(res_eur.availability, "out_of_stock")
        self.assertFalse(res_eur.in_stock)

        html_rub = """
        <html><body>
          <h1>Smart Kettle</h1>
          <span class="price">4 990 ₽</span>
        </body></html>
        """
        res_rub = extract_from_html(html_rub)
        self.assertEqual(res_rub.extractor_source, "regex-fallback")
        self.assertEqual(res_rub.currency, "RUB")
        self.assertEqual(res_rub.price, Decimal("4990"))
        self.assertEqual(res_rub.availability, "unknown")
        self.assertIsNone(res_rub.in_stock)

    def test_12_decimal_precision_and_thousand_separators(self) -> None:
        self.assertEqual(parse_price_and_currency("$1,299.00"), (Decimal("1299.00"), "USD"))
        self.assertEqual(parse_price_and_currency("19,95 EUR"), (Decimal("19.95"), "EUR"))
        self.assertEqual(parse_price_and_currency("1.234.567 ₽"), (Decimal("1234567"), "RUB"))
        self.assertEqual(parse_price_and_currency(19.99, "USD"), (Decimal("19.99"), "USD"))
        self.assertIsNone(parse_price_and_currency("-10.00", "USD")[0])
        self.assertEqual(parse_price_and_currency(-5, "USD"), (None, "USD"))

    def test_13_malformed_jsonld_falls_back_to_opengraph(self) -> None:
        html = """
        <html>
          <head>
            <script type="application/ld+json">{ invalid json: [ </script>
            <meta property="og:title" content="USB Audio Interface" />
            <meta property="product:price:amount" content="159.00" />
            <meta property="product:price:currency" content="USD" />
            <meta property="product:availability" content="instock" />
          </head>
        </html>
        """
        res = extract_from_html(html)
        self.assertEqual(res.extractor_source, "opengraph")
        self.assertEqual(res.name, "USB Audio Interface")
        self.assertEqual(res.price, Decimal("159.00"))
        self.assertTrue(res.in_stock)

    def test_14_priority_order_jsonld_over_opengraph_and_microdata(self) -> None:
        html = """
        <html>
          <head>
            <script type="application/ld+json">
            {"@type": "Product", "name": "JSON-LD Item", "offers": {"@type": "Offer", "price": "99.00", "priceCurrency": "USD", "availability": "InStock"}}
            </script>
            <meta property="product:price:amount" content="105.00" />
            <meta property="product:price:currency" content="USD" />
          </head>
          <body>
            <div itemscope itemtype="https://schema.org/Product">
              <span itemprop="name">Microdata Item</span>
              <span itemprop="price">110.00</span>
            </div>
          </body>
        </html>
        """
        res = extract_from_html(html)
        self.assertEqual(res.extractor_source, "json-ld")
        self.assertEqual(res.name, "JSON-LD Item")
        self.assertEqual(res.price, Decimal("99.00"))

    def test_15_availability_normalization_matrix(self) -> None:
        self.assertEqual(normalize_availability("https://schema.org/InStock"), ("in_stock", True))
        self.assertEqual(normalize_availability("LimitedAvailability"), ("in_stock", True))
        self.assertEqual(normalize_availability("OnlineOnly"), ("in_stock", True))
        self.assertEqual(normalize_availability("https://schema.org/OutOfStock"), ("out_of_stock", False))
        self.assertEqual(normalize_availability("SoldOut"), ("out_of_stock", False))
        self.assertEqual(normalize_availability("Discontinued"), ("out_of_stock", False))
        self.assertEqual(normalize_availability("PreOrder"), ("preorder", False))
        self.assertEqual(normalize_availability("BackOrder"), ("backorder", False))
        self.assertEqual(normalize_availability("CustomFlag"), ("unknown", None))

    def test_16_extract_from_local_file_and_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            html_path = Path(tmpdir) / "product.html"
            html_path.write_text(
                '<html><head><meta property="og:title" content="Desk Lamp"><meta property="og:price:amount" content="42.50"><meta property="og:price:currency" content="USD"></head></html>',
                encoding="utf-8",
            )
            res = extract_from_target(str(html_path))
            self.assertIsNone(res.error)
            self.assertEqual(res.name, "Desk Lamp")
            self.assertEqual(res.price, Decimal("42.50"))
            self.assertEqual(res.currency, "USD")

            missing = extract_from_target(str(Path(tmpdir) / "does_not_exist.html"))
            self.assertIsNotNone(missing.error)
            self.assertIsNone(missing.price)

    def test_17_extract_from_url_mocked_gzip_response(self) -> None:
        raw_html = (
            '<html><head><script type="application/ld+json">'
            '{"@type":"Product","name":"Gzipped Item","offers":{"@type":"Offer","price":"59.99","priceCurrency":"GBP","availability":"InStock"}}'
            "</script></head></html>"
        ).encode("utf-8")
        gz_bytes = gzip.compress(raw_html)
        mock_resp = _MockHTTPResponse(
            body=gz_bytes,
            headers={"Content-Type": "text/html; charset=utf-8", "Content-Encoding": "gzip"},
            final_url="https://shop.example.com/item-final",
        )
        with mock.patch("urllib.request.urlopen", return_value=mock_resp) as mocked:
            res = extract_from_url("https://shop.example.com/item")
            mocked.assert_called_once()

        self.assertIsNone(res.error)
        self.assertEqual(res.extractor_source, "json-ld")
        self.assertEqual(res.name, "Gzipped Item")
        self.assertEqual(res.currency, "GBP")
        self.assertEqual(res.price, Decimal("59.99"))
        self.assertTrue(res.in_stock)
        self.assertEqual(res.url, "https://shop.example.com/item-final")

    def test_18_cli_json_table_and_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            ok_file = tmp / "ok.html"
            ok_file.write_text(
                '<script type="application/ld+json">{"@type":"Product","name":"CLI Widget","offers":{"@type":"Offer","price":"19.99","priceCurrency":"USD","availability":"InStock"}}</script>',
                encoding="utf-8",
            )
            empty_file = tmp / "empty.html"
            empty_file.write_text("<html><body>No price here</body></html>", encoding="utf-8")
            out_json = tmp / "out.json"

            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                code_table = cli.main([str(ok_file), "--format", "table"])
            self.assertEqual(code_table, 0)
            table_out = buf.getvalue()
            self.assertIn("CLI Widget", table_out)
            self.assertIn("19.99", table_out)
            self.assertIn("json-ld", table_out)

            code_json = cli.main([str(ok_file), "--format", "json", "--output", str(out_json)])
            self.assertEqual(code_json, 0)
            parsed = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(len(parsed), 1)
            self.assertEqual(parsed[0]["name"], "CLI Widget")
            self.assertEqual(parsed[0]["price"], "19.99")
            self.assertEqual(parsed[0]["currency"], "USD")
            self.assertTrue(parsed[0]["in_stock"])
            self.assertEqual(parsed[0]["extractor_source"], "json-ld")

            buf2 = io.StringIO()
            with mock.patch("sys.stdout", buf2):
                code_missing = cli.main([str(empty_file), "--format", "json", "--fail-on-missing"])
            self.assertEqual(code_missing, 1)


if __name__ == "__main__":
    unittest.main()
