# ecommerce-jsonld-price-extractor-cli-lite

**Zero-dependency Python 3.9+ CLI & library for extracting structured product pricing, currency, and stock availability from e-commerce HTML pages and URLs.**

Built 100% with the Python standard library (`html.parser`, `json`, `re`, `decimal`, `urllib.request`, `gzip`, `argparse`). No `pip install`, no Playwright/Selenium, and no third-party parsers required.

---

## Features

- **4-tier extraction cascade** with deterministic priority:
  1. **JSON-LD** (`<script type="application/ld+json">` `Product`, `Offer`, `AggregateOffer`, `@graph` & `@id` reference resolution, `priceSpecification`, multi-offer `InStock` preference)
  2. **OpenGraph & Meta tags** (`product:price:amount`, `og:price:amount`, `product:price:currency`, `og:price:currency`, `product:availability`, `og:availability`)
  3. **Schema.org Microdata** (`itemscope` / `itemtype` / `itemprop` parsing for nested `Product` and `Offer` blocks, including void `<meta>` and `<link>` tags)
  4. **Regex price fallback** (localized currency symbol & ISO code detection across price elements and visible page text, skipping `<script>`, `<style>`, and strikethrough/old-price tags)
- **Exact `Decimal` arithmetic** — handles US/UK (`1,299.00`), European (`1.299,50 €`), and space-delimited (`4 990 ₽`) prices without floating-point rounding drift.
- **Normalized availability status** — maps schema.org URLs and OpenGraph tokens to `in_stock`, `out_of_stock`, `preorder`, `backorder`, or `unknown` plus a boolean `in_stock` field.
- **Local HTML files & remote URLs** — supports local paths, `file://` URIs, batch lists via `--file` (or `stdin`), and `http://`/`https://` fetches with transparent `gzip` decompression.
- **Clean JSON & Table output** — output aligned tables for terminal inspection or structured JSON for scripts and CI pipelines (`--fail-on-missing`).

---

## Quick Start

Parse a local HTML file and print an aligned table:

```bash
python cli.py product.html
```

Parse one or more URLs and emit structured JSON:

```bash
python cli.py https://example.com/product-a https://example.com/product-b --format json
```

Read targets from a list file (`targets.txt`) and save JSON output:

```bash
python cli.py --file targets.txt --format json --output prices.json --fail-on-missing
```

Pipe targets via `stdin`:

```bash
cat targets.txt | python cli.py --file - --format json --compact
```

Use directly as a Python module:

```python
from extractor import extract_from_html, extract_from_file

result = extract_from_file("product.html")
print(result.to_dict())
```

---

## Example Output

### Table (`--format table`)

```text
Product Name            | Currency | Price  | Availability | In Stock | Extractor | Source
------------------------+----------+--------+--------------+----------+-----------+------------------
Mechanical Keyboard 75% | USD      | 129.99 | in_stock     | yes      | json-ld   | product.html
```

### JSON (`--format json`)

```json
[
  {
    "source": "product.html",
    "name": "Mechanical Keyboard 75%",
    "currency": "USD",
    "price": "129.99",
    "availability": "in_stock",
    "in_stock": true,
    "extractor_source": "json-ld",
    "url": "https://shop.example.com/kb-75",
    "error": null
  }
]
```

---

## CLI Options

| Flag | Default | Description |
| :--- | :--- | :--- |
| `targets` | — | Positional local HTML file paths or `http(s)://` URLs |
| `-f`, `--file` | — | Read targets from a file (`#` comments ignored, `-` for `stdin`) |
| `--format` | `table` | Output format: `table` or `json` |
| `--compact` | `false` | Emit single-line compact JSON when `--format json` is used |
| `-o`, `--output` | `stdout` | Write formatted output to a UTF-8 file |
| `-t`, `--timeout` | `15.0` | HTTP request timeout in seconds |
| `--user-agent` | `ecommerce-jsonld-price-extractor-cli-lite/1.0` | Custom HTTP `User-Agent` header |
| `--fail-on-missing` | `false` | Exit with code `1` if any target errors or yields no price |
| `-V`, `--version` | — | Print CLI version and exit |

---

## Running Tests

Run the full offline unit test suite (zero network calls):

```bash
py -m unittest discover -s tests -v
```

---

## Need 24/7 Automated Price & Restock Monitoring in Telegram?

This Lite CLI is built for one-off local HTML parsing, ad-hoc URL checks, and lightweight scripts. If you need a **24/7 self-hosted monitoring service** that continuously tracks product pages and sends instant alerts directly to Telegram:

| Capability | Lite CLI (This Repo) | Pro: Telegram Price & Stock Monitor Bot |
| :--- | :--- | :--- |
| JSON-LD / OpenGraph / Microdata / Regex extraction | Yes (`Decimal`) | Yes (`Decimal`) |
| Output formats | One-off CLI table / JSON | Automated 24/7 Telegram notifications |
| Persistent price history | None | SQLite WAL history (`min` / `max` / `avg` tracking) |
| Price drop & target alerts | Manual diffing | Target price & percentage drop (`%`) alert rules |
| Restock alerts | Single-run status check | Instant back-in-stock notifications |
| Anti-spam protection | — | Configurable per-item alert cooldowns |
| Management & operations | CLI arguments | In-chat Telegram admin commands |
| Verification | 18 offline unit tests | 24 comprehensive self-tests |

- **Get the Pro Version (Telegram Price & Stock Monitor Bot):** https://funpay.com/lots/offer?id=82602607

---

## License

MIT License — see [LICENSE](LICENSE).
