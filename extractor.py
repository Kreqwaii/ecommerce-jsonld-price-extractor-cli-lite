"""Pure-stdlib e-commerce product price, currency, and stock extractor."""

from __future__ import annotations

import gzip
import html
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

__all__ = [
    "ExtractionResult",
    "extract_from_file",
    "extract_from_html",
    "extract_from_target",
    "extract_from_url",
    "normalize_availability",
    "parse_price_and_currency",
]

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; ecommerce-jsonld-price-extractor-cli-lite/1.0)"
)
DEFAULT_TIMEOUT = 15.0

VOID_HTML_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

IGNORED_TEXT_TAGS = frozenset({"script", "style", "noscript", "template"})

SUPPORTED_ISO_CURRENCIES = frozenset(
    {
        "USD",
        "EUR",
        "GBP",
        "RUB",
        "RUR",
        "JPY",
        "CNY",
        "RMB",
        "CAD",
        "AUD",
        "CHF",
        "SEK",
        "NOK",
        "DKK",
        "PLN",
        "CZK",
        "HUF",
        "TRY",
        "INR",
        "BRL",
        "MXN",
        "KRW",
        "SGD",
        "HKD",
        "NZD",
        "UAH",
        "KZT",
        "BYN",
        "AED",
        "SAR",
        "ILS",
        "ZAR",
        "THB",
        "VND",
        "IDR",
        "MYR",
        "PHP",
        "TWD",
    }
)

CURRENCY_PREFIX_MAP: Tuple[Tuple[str, str], ...] = (
    ("US$", "USD"),
    ("CA$", "CAD"),
    ("AU$", "AUD"),
    ("A$", "AUD"),
    ("NZ$", "NZD"),
    ("HK$", "HKD"),
    ("S$", "SGD"),
    ("CN¥", "CNY"),
    ("RMB", "CNY"),
    ("руб.", "RUB"),
    ("руб", "RUB"),
    ("zł", "PLN"),
    ("€", "EUR"),
    ("£", "GBP"),
    ("₽", "RUB"),
    ("¥", "JPY"),
    ("₩", "KRW"),
    ("₹", "INR"),
    ("₴", "UAH"),
    ("₺", "TRY"),
    ("$", "USD"),
)

_CURRENCY_TOKEN_PATTERN = (
    r"(?:US\$|CA\$|AU\$|A\$|NZ\$|HK\$|S\$|CN¥|RMB|[$€£₽¥₩₹₴₺]|zł|руб\.?|"
    + "|".join(sorted(SUPPORTED_ISO_CURRENCIES, key=len, reverse=True))
    + r")"
)
_NUMBER_TOKEN_PATTERN = (
    r"(?:\d{1,3}(?:[ \u00a0\u202f'.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,4})?)"
)

PRICE_BEFORE_CURRENCY_RE = re.compile(
    rf"(?<![\w.])({_NUMBER_TOKEN_PATTERN})\s*({_CURRENCY_TOKEN_PATTERN})(?![\w])",
    re.IGNORECASE,
)
CURRENCY_BEFORE_PRICE_RE = re.compile(
    rf"(?<![\w.])({_CURRENCY_TOKEN_PATTERN})\s*({_NUMBER_TOKEN_PATTERN})(?![\w])",
    re.IGNORECASE,
)
STANDALONE_NUMBER_RE = re.compile(_NUMBER_TOKEN_PATTERN)

STRIKETHROUGH_HINT_RE = re.compile(
    r"(?:old[-_ ]?price|was[-_ ]?price|compare[-_ ]?at|strikethrough|strike|msrp|list[-_ ]?price|regular[-_ ]?price)",
    re.IGNORECASE,
)
PRICE_CLASS_HINT_RE = re.compile(
    r"(?:price|amount)",
    re.IGNORECASE,
)


@dataclass
class ExtractionResult:
    """Normalized e-commerce product extraction result."""

    source: str
    name: Optional[str] = None
    currency: Optional[str] = None
    price: Optional[Decimal] = None
    availability: str = "unknown"
    in_stock: Optional[bool] = None
    extractor_source: Optional[str] = None
    url: Optional[str] = None
    error: Optional[str] = None

    def price_str(self) -> Optional[str]:
        if self.price is None:
            return None
        return format(self.price, "f")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "name": self.name,
            "currency": self.currency,
            "price": self.price_str(),
            "availability": self.availability,
            "in_stock": self.in_stock,
            "extractor_source": self.extractor_source,
            "url": self.url,
            "error": self.error,
        }


def _clean_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    unescaped = html.unescape(str(value))
    collapsed = re.sub(r"\s+", " ", unescaped).strip()
    return collapsed or None


def normalize_currency(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    cleaned = raw.strip()
    upper = cleaned.upper()
    if upper == "RUR":
        return "RUB"
    if upper == "RMB":
        return "CNY"
    if upper in SUPPORTED_ISO_CURRENCIES:
        return upper
    lower = cleaned.lower()
    for token, iso_code in CURRENCY_PREFIX_MAP:
        if lower == token.lower():
            return iso_code
    for token, iso_code in CURRENCY_PREFIX_MAP:
        if token.lower() in lower:
            return iso_code
    iso_match = re.search(r"\b([A-Za-z]{3})\b", upper)
    if iso_match and iso_match.group(1) in SUPPORTED_ISO_CURRENCIES:
        code = iso_match.group(1)
        return "RUB" if code == "RUR" else ("CNY" if code == "RMB" else code)
    return None


def _parse_numeric_string(num_str: str) -> Optional[Decimal]:
    s = num_str.strip()
    s = s.replace("\u00a0", "").replace("\u202f", "").replace(" ", "").replace("'", "").replace("\u2019", "")
    if not s:
        return None

    has_comma = "," in s
    has_dot = "." in s

    if has_comma and has_dot:
        last_comma = s.rfind(",")
        last_dot = s.rfind(".")
        if last_comma > last_dot:
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_comma:
        parts = s.split(",")
        if len(parts) > 2:
            if all(len(p) == 3 for p in parts[1:]):
                s = "".join(parts)
            else:
                return None
        else:
            left, right = parts
            if len(right) == 3 and len(left) >= 1 and left.isdigit():
                s = left + right
            elif 1 <= len(right) <= 4:
                s = f"{left}.{right}"
            else:
                return None
    elif has_dot:
        parts = s.split(".")
        if len(parts) > 2:
            if all(len(p) == 3 for p in parts[1:]):
                s = "".join(parts)
            else:
                return None

    if not re.fullmatch(r"\d+(?:\.\d+)?", s):
        return None
    try:
        dec = Decimal(s)
    except InvalidOperation:
        return None
    if not dec.is_finite() or dec < Decimal("0"):
        return None
    return dec


def parse_price_and_currency(
    raw_value: Any,
    explicit_currency: Optional[str] = None,
) -> Tuple[Optional[Decimal], Optional[str]]:
    """Parse a price value (int, float, Decimal, or localized string) and optional currency."""
    detected_currency = normalize_currency(explicit_currency)

    if raw_value is None or isinstance(raw_value, bool):
        return None, detected_currency

    if isinstance(raw_value, Decimal):
        if raw_value.is_finite() and raw_value >= Decimal("0"):
            return raw_value, detected_currency
        return None, detected_currency

    if isinstance(raw_value, int):
        dec = Decimal(raw_value)
        return (dec, detected_currency) if dec >= Decimal("0") else (None, detected_currency)

    if isinstance(raw_value, float):
        try:
            dec = Decimal(str(raw_value))
        except InvalidOperation:
            return None, detected_currency
        if dec.is_finite() and dec >= Decimal("0"):
            return dec, detected_currency
        return None, detected_currency

    text = _clean_text(str(raw_value))
    if not text:
        return None, detected_currency

    if re.match(r"^-\s*(?:[$€£₽¥₩₹₴₺]|\d)", text):
        return None, detected_currency

    m_curr_first = CURRENCY_BEFORE_PRICE_RE.search(text)
    if m_curr_first:
        if m_curr_first.start() == 0 or text[m_curr_first.start() - 1] != "-":
            cur = detected_currency or normalize_currency(m_curr_first.group(1))
            dec = _parse_numeric_string(m_curr_first.group(2))
            if dec is not None:
                return dec, cur

    m_price_first = PRICE_BEFORE_CURRENCY_RE.search(text)
    if m_price_first:
        if m_price_first.start() == 0 or text[m_price_first.start() - 1] != "-":
            dec = _parse_numeric_string(m_price_first.group(1))
            cur = detected_currency or normalize_currency(m_price_first.group(2))
            if dec is not None:
                return dec, cur

    num_match = STANDALONE_NUMBER_RE.search(text)
    if num_match:
        if num_match.start() == 0 or text[num_match.start() - 1] != "-":
            dec = _parse_numeric_string(num_match.group(0))
            cur = detected_currency or normalize_currency(text)
            if dec is not None:
                return dec, cur

    return None, detected_currency


def normalize_availability(raw: Optional[str]) -> Tuple[str, Optional[bool]]:
    """Normalize schema.org / OpenGraph / text availability into (status, in_stock)."""
    if not raw:
        return "unknown", None

    text = _clean_text(raw)
    if not text:
        return "unknown", None

    if "/" in text:
        text = text.rstrip("/").rsplit("/", 1)[-1]

    compact = re.sub(r"[\s_\-]+", "", text).lower()

    if compact in {
        "instock",
        "available",
        "availablefororder",
        "limitedavailability",
        "onlineonly",
        "instoreonly",
        "true",
        "yes",
    }:
        return "in_stock", True

    if compact in {
        "outofstock",
        "oos",
        "soldout",
        "discontinued",
        "unavailable",
        "currentlyunavailable",
        "false",
        "no",
    }:
        return "out_of_stock", False

    if compact in {"preorder", "presale"}:
        return "preorder", False

    if compact in {"backorder", "pending"}:
        return "backorder", False

    lower = text.lower()
    if "out of stock" in lower or "sold out" in lower or "unavailable" in lower:
        return "out_of_stock", False
    if "in stock" in lower:
        return "in_stock", True
    if "pre-order" in lower or "preorder" in lower:
        return "preorder", False
    if "backorder" in lower or "back-order" in lower:
        return "backorder", False

    return "unknown", None


@dataclass
class _MicrodataNode:
    item_type: Optional[str]
    props: Dict[str, List[Any]]


@dataclass
class _ActiveScope:
    tag: str
    itemprop: Optional[str]
    node: Optional[_MicrodataNode]
    capture_prop: Optional[str]
    prop_attr_value: Optional[str]
    prop_text_parts: List[str]


class _ProductHTMLParser(HTMLParser):
    """Single-pass HTML parser collecting JSON-LD, OpenGraph, Microdata, and text hints."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.jsonld_blocks: List[str] = []
        self.meta_tags: List[Dict[str, str]] = []
        self.canonical_url: Optional[str] = None
        self.title_text: Optional[str] = None
        self.h1_text: Optional[str] = None
        self.microdata_roots: List[_MicrodataNode] = []
        self.flat_microdata_props: Dict[str, List[str]] = {}
        self.price_hint_snippets: List[str] = []
        self.body_text_parts: List[str] = []

        self._in_jsonld = False
        self._jsonld_parts: List[str] = []

        self._in_title = False
        self._title_parts: List[str] = []

        self._in_h1 = False
        self._h1_parts: List[str] = []

        self._ignored_depth = 0
        self._strike_depth = 0
        self._price_hint_depth = 0
        self._price_hint_is_strike = False
        self._price_hint_parts: List[str] = []

        self._tag_stack: List[str] = []
        self._scope_stack: List[_ActiveScope] = []
        self._node_stack: List[_MicrodataNode] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag_l = tag.lower()
        attr_map: Dict[str, str] = {}
        for k, v in attrs:
            if k:
                attr_map[k.lower()] = v if v is not None else ""

        is_void = tag_l in VOID_HTML_TAGS

        if tag_l == "script":
            script_type = attr_map.get("type", "").split(";", 1)[0].strip().lower()
            if script_type == "application/ld+json":
                self._in_jsonld = True
                self._jsonld_parts = []
            self._ignored_depth += 1
        elif tag_l in IGNORED_TEXT_TAGS:
            self._ignored_depth += 1

        if tag_l == "meta":
            self.meta_tags.append(attr_map)

        if tag_l == "link":
            rel_vals = {r.lower() for r in attr_map.get("rel", "").split()}
            if "canonical" in rel_vals and attr_map.get("href") and not self.canonical_url:
                self.canonical_url = attr_map["href"].strip()

        if tag_l == "title" and self.title_text is None:
            self._in_title = True
            self._title_parts = []

        if tag_l == "h1" and self.h1_text is None:
            self._in_h1 = True
            self._h1_parts = []

        if self._ignored_depth == 0 and not is_void:
            class_id = f"{attr_map.get('class', '')} {attr_map.get('id', '')} {attr_map.get('data-testid', '')}"
            if self._strike_depth > 0 or tag_l in {"s", "del", "strike"} or STRIKETHROUGH_HINT_RE.search(class_id):
                self._strike_depth += 1

            if self._price_hint_depth > 0:
                self._price_hint_depth += 1
            elif PRICE_CLASS_HINT_RE.search(class_id) and self._strike_depth == 0:
                self._price_hint_depth = 1
                self._price_hint_is_strike = False
                self._price_hint_parts = []

        # Microdata handling
        has_scope = "itemscope" in attr_map
        itemprop_raw = attr_map.get("itemprop", "").strip()
        itemprop_names = [p.strip() for p in itemprop_raw.split() if p.strip()] if itemprop_raw else []
        itemtype = attr_map.get("itemtype", "").strip() or None

        attr_val = self._extract_microdata_attr_value(tag_l, attr_map)

        new_node: Optional[_MicrodataNode] = None
        if has_scope:
            new_node = _MicrodataNode(item_type=itemtype, props={})
            if itemprop_names and self._node_stack:
                parent_node = self._node_stack[-1]
                for pname in itemprop_names:
                    parent_node.props.setdefault(pname, []).append(new_node)
            else:
                self.microdata_roots.append(new_node)

        if is_void:
            if itemprop_names and not has_scope and attr_val is not None:
                if self._node_stack:
                    parent_node = self._node_stack[-1]
                    for pname in itemprop_names:
                        parent_node.props.setdefault(pname, []).append(attr_val)
                for pname in itemprop_names:
                    self.flat_microdata_props.setdefault(pname, []).append(attr_val)
            return

        self._tag_stack.append(tag_l)
        if new_node is not None:
            self._node_stack.append(new_node)

        capture_prop = itemprop_raw if (itemprop_names and not has_scope) else None
        self._scope_stack.append(
            _ActiveScope(
                tag=tag_l,
                itemprop=itemprop_raw or None,
                node=new_node,
                capture_prop=capture_prop,
                prop_attr_value=attr_val,
                prop_text_parts=[],
            )
        )

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if tag_l in VOID_HTML_TAGS:
            return

        if tag_l == "script" and self._in_jsonld:
            block = "".join(self._jsonld_parts).strip()
            if block:
                self.jsonld_blocks.append(block)
            self._in_jsonld = False
            self._jsonld_parts = []

        if tag_l in IGNORED_TEXT_TAGS and self._ignored_depth > 0:
            self._ignored_depth -= 1
            return_ignored = True
        else:
            return_ignored = False

        if tag_l == "title" and self._in_title:
            self._in_title = False
            cleaned = _clean_text("".join(self._title_parts))
            if cleaned:
                self.title_text = cleaned

        if tag_l == "h1" and self._in_h1:
            self._in_h1 = False
            cleaned = _clean_text("".join(self._h1_parts))
            if cleaned:
                self.h1_text = cleaned

        if not return_ignored and self._ignored_depth == 0:
            if self._strike_depth > 0:
                self._strike_depth -= 1
            if self._price_hint_depth > 0:
                self._price_hint_depth -= 1
                if self._price_hint_depth == 0:
                    snippet = _clean_text("".join(self._price_hint_parts))
                    if snippet and not self._price_hint_is_strike:
                        self.price_hint_snippets.append(snippet)
                    self._price_hint_parts = []
                    self._price_hint_is_strike = False

        # Pop matching scope(s) cleanly
        if self._scope_stack:
            idx = len(self._scope_stack) - 1
            while idx >= 0 and self._scope_stack[idx].tag != tag_l:
                idx -= 1
            if idx >= 0:
                while len(self._scope_stack) > idx:
                    scope = self._scope_stack.pop()
                    if self._tag_stack:
                        self._tag_stack.pop()
                    if scope.node is not None and self._node_stack and self._node_stack[-1] is scope.node:
                        self._node_stack.pop()
                    if scope.capture_prop:
                        val = scope.prop_attr_value
                        if val is None:
                            val = _clean_text("".join(scope.prop_text_parts))
                        if val is not None:
                            for pname in scope.capture_prop.split():
                                if self._node_stack:
                                    self._node_stack[-1].props.setdefault(pname, []).append(val)
                                self.flat_microdata_props.setdefault(pname, []).append(val)

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._jsonld_parts.append(data)
        if self._in_title:
            self._title_parts.append(data)
        if self._in_h1:
            self._h1_parts.append(data)

        if self._ignored_depth == 0:
            if self._strike_depth == 0:
                self.body_text_parts.append(data)
            if self._price_hint_depth > 0 and self._strike_depth == 0:
                self._price_hint_parts.append(data)
            for scope in self._scope_stack:
                if scope.capture_prop:
                    scope.prop_text_parts.append(data)

    @staticmethod
    def _extract_microdata_attr_value(tag_l: str, attr_map: Dict[str, str]) -> Optional[str]:
        if "content" in attr_map and attr_map["content"].strip():
            return attr_map["content"].strip()
        if tag_l in {"a", "area", "link"} and attr_map.get("href"):
            return attr_map["href"].strip()
        if tag_l in {"img", "audio", "video", "source", "embed", "iframe"} and attr_map.get("src"):
            return attr_map["src"].strip()
        if tag_l == "time" and attr_map.get("datetime"):
            return attr_map["datetime"].strip()
        if tag_l in {"data", "meter"} and attr_map.get("value"):
            return attr_map["value"].strip()
        return None


def _strip_jsonld_wrappers(raw: str) -> str:
    s = raw.strip()
    s = re.sub(r"^\s*<!--", "", s)
    s = re.sub(r"-->\s*$", "", s)
    s = re.sub(r"^\s*(?://|/\*)?\s*<!\[CDATA\[(?:\s*\*/)?", "", s)
    s = re.sub(r"(?://|/\*)?\s*\]\]>(?:\s*\*/)?\s*$", "", s)
    return s.strip()


def _schema_types(node: Dict[str, Any]) -> List[str]:
    raw_type = node.get("@type")
    items = raw_type if isinstance(raw_type, list) else ([raw_type] if raw_type else [])
    types: List[str] = []
    for item in items:
        if isinstance(item, str):
            short = item.rstrip("/").rsplit("/", 1)[-1].lower()
            types.append(short)
    return types


def _collect_jsonld_nodes(obj: Any, id_index: Dict[str, Dict[str, Any]], all_nodes: List[Dict[str, Any]]) -> None:
    if isinstance(obj, list):
        for item in obj:
            _collect_jsonld_nodes(item, id_index, all_nodes)
    elif isinstance(obj, dict):
        all_nodes.append(obj)
        node_id = obj.get("@id")
        if isinstance(node_id, str) and node_id:
            id_index[node_id] = obj
        for val in obj.values():
            if isinstance(val, (dict, list)):
                _collect_jsonld_nodes(val, id_index, all_nodes)


def _resolve_jsonld_ref(val: Any, id_index: Dict[str, Dict[str, Any]]) -> Any:
    if isinstance(val, dict) and set(val.keys()) == {"@id"} and isinstance(val["@id"], str):
        return id_index.get(val["@id"], val)
    return val


def _extract_offer_tuple(
    offer_raw: Any,
    id_index: Dict[str, Dict[str, Any]],
) -> Tuple[Optional[Decimal], Optional[str], str, Optional[bool], Optional[str]]:
    offer = _resolve_jsonld_ref(offer_raw, id_index)
    if not isinstance(offer, dict):
        return None, None, "unknown", None, None

    # Handle nested AggregateOffer -> offers list if needed
    explicit_currency = _clean_text(offer.get("priceCurrency")) if isinstance(offer.get("priceCurrency"), str) else None
    avail_raw = offer.get("availability")
    avail_status, in_stock = normalize_availability(avail_raw if isinstance(avail_raw, str) else None)
    offer_url = _clean_text(offer.get("url")) if isinstance(offer.get("url"), str) else None

    price_candidates: List[Tuple[Any, Optional[str]]] = []
    for key in ("price", "lowPrice", "highPrice"):
        if key in offer and offer[key] is not None:
            price_candidates.append((offer[key], explicit_currency))

    price_spec = offer.get("priceSpecification")
    specs = price_spec if isinstance(price_spec, list) else ([price_spec] if isinstance(price_spec, dict) else [])
    for spec in specs:
        resolved_spec = _resolve_jsonld_ref(spec, id_index)
        if isinstance(resolved_spec, dict):
            spec_curr = (
                _clean_text(resolved_spec.get("priceCurrency"))
                if isinstance(resolved_spec.get("priceCurrency"), str)
                else explicit_currency
            )
            for key in ("price", "minPrice", "maxPrice"):
                if key in resolved_spec and resolved_spec[key] is not None:
                    price_candidates.append((resolved_spec[key], spec_curr))

    for raw_p, curr_hint in price_candidates:
        dec, cur = parse_price_and_currency(raw_p, curr_hint)
        if dec is not None:
            return dec, cur, avail_status, in_stock, offer_url

    nested_offers = offer.get("offers")
    if nested_offers is not None:
        nested_list = nested_offers if isinstance(nested_offers, list) else [nested_offers]
        best = _pick_best_offer(nested_list, id_index)
        if best is not None:
            p, c, av, st, u = best
            return p, c or normalize_currency(explicit_currency), av if av != "unknown" else avail_status, st if st is not None else in_stock, u or offer_url

    return None, normalize_currency(explicit_currency), avail_status, in_stock, offer_url


def _pick_best_offer(
    offers_raw: List[Any],
    id_index: Dict[str, Dict[str, Any]],
) -> Optional[Tuple[Optional[Decimal], Optional[str], str, Optional[bool], Optional[str]]]:
    parsed_offers = [_extract_offer_tuple(o, id_index) for o in offers_raw]
    if not parsed_offers:
        return None
    # Priority: in_stock with price -> any with price -> in_stock without price -> first
    for item in parsed_offers:
        if item[0] is not None and item[3] is True:
            return item
    for item in parsed_offers:
        if item[0] is not None:
            return item
    for item in parsed_offers:
        if item[3] is True:
            return item
    return parsed_offers[0]


def _extract_from_jsonld(
    blocks: List[str],
) -> Optional[Tuple[Optional[str], Optional[str], Optional[Decimal], str, Optional[bool], Optional[str]]]:
    id_index: Dict[str, Dict[str, Any]] = {}
    all_nodes: List[Dict[str, Any]] = []

    for block in blocks:
        cleaned = _strip_jsonld_wrappers(block)
        if not cleaned:
            continue
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            continue
        _collect_jsonld_nodes(parsed, id_index, all_nodes)

    if not all_nodes:
        return None

    product_types = {"product", "individualproduct", "productmodel", "productgroup"}
    offer_types = {"offer", "aggregateoffer"}

    product_nodes = [n for n in all_nodes if any(t in product_types for t in _schema_types(n))]
    candidates: List[Tuple[Optional[str], Optional[str], Optional[Decimal], str, Optional[bool], Optional[str]]] = []

    for prod in product_nodes:
        name = _clean_text(prod.get("name")) if isinstance(prod.get("name"), str) else None
        prod_url = _clean_text(prod.get("url")) if isinstance(prod.get("url"), str) else None
        offers_val = prod.get("offers")
        if offers_val is not None:
            offers_list = offers_val if isinstance(offers_val, list) else [offers_val]
            best = _pick_best_offer(offers_list, id_index)
            if best is not None:
                price, curr, avail, in_stock, offer_url = best
                candidates.append((name, curr, price, avail, in_stock, offer_url or prod_url))
                continue
        if name or prod_url:
            candidates.append((name, None, None, "unknown", None, prod_url))

    for item in candidates:
        if item[2] is not None:
            return item

    # Standalone Offer / AggregateOffer fallback in JSON-LD
    offer_nodes = [n for n in all_nodes if any(t in offer_types for t in _schema_types(n))]
    if offer_nodes:
        best = _pick_best_offer(offer_nodes, id_index)
        if best is not None and best[0] is not None:
            price, curr, avail, in_stock, offer_url = best
            fallback_name = candidates[0][0] if candidates else None
            return fallback_name, curr, price, avail, in_stock, offer_url

    return candidates[0] if candidates else None


def _extract_from_opengraph(
    meta_tags: List[Dict[str, str]],
) -> Optional[Tuple[Optional[str], Optional[str], Optional[Decimal], str, Optional[bool], Optional[str]]]:
    meta_map: Dict[str, str] = {}
    for attrs in meta_tags:
        key = (attrs.get("property") or attrs.get("name") or "").strip().lower()
        val = attrs.get("content", "").strip()
        if key and val and key not in meta_map:
            meta_map[key] = val

    raw_amount = (
        meta_map.get("product:price:amount")
        or meta_map.get("og:price:amount")
        or meta_map.get("product:sale_price:amount")
        or meta_map.get("product:price")
    )
    raw_currency = (
        meta_map.get("product:price:currency")
        or meta_map.get("og:price:currency")
        or meta_map.get("product:sale_price:currency")
    )
    raw_avail = (
        meta_map.get("product:availability")
        or meta_map.get("og:availability")
        or meta_map.get("availability")
    )
    name = _clean_text(meta_map.get("og:title") or meta_map.get("twitter:title"))
    url = _clean_text(meta_map.get("og:url"))

    price, currency = parse_price_and_currency(raw_amount, raw_currency)
    avail, in_stock = normalize_availability(raw_avail)

    if price is None and avail == "unknown":
        return None
    return name, currency, price, avail, in_stock, url


def _find_microdata_nodes(nodes: List[_MicrodataNode], target_types: set) -> List[_MicrodataNode]:
    found: List[_MicrodataNode] = []

    def _walk(node: _MicrodataNode) -> None:
        if node.item_type:
            short = node.item_type.rstrip("/").rsplit("/", 1)[-1].lower()
            if short in target_types:
                found.append(node)
        for prop_vals in node.props.values():
            for v in prop_vals:
                if isinstance(v, _MicrodataNode):
                    _walk(v)

    for root in nodes:
        _walk(root)
    return found


def _first_str_prop(node: _MicrodataNode, keys: Tuple[str, ...]) -> Optional[str]:
    for key in keys:
        for val in node.props.get(key, []):
            if isinstance(val, str):
                cleaned = _clean_text(val)
                if cleaned:
                    return cleaned
    return None


def _extract_from_microdata(
    roots: List[_MicrodataNode],
    flat_props: Dict[str, List[str]],
) -> Optional[Tuple[Optional[str], Optional[str], Optional[Decimal], str, Optional[bool], Optional[str]]]:
    product_nodes = _find_microdata_nodes(roots, {"product", "individualproduct", "productmodel"})
    offer_nodes_global = _find_microdata_nodes(roots, {"offer", "aggregateoffer"})

    for prod in product_nodes:
        name = _first_str_prop(prod, ("name",))
        prod_url = _first_str_prop(prod, ("url",))

        offer_nodes: List[_MicrodataNode] = []
        for raw_offer in prod.props.get("offers", []):
            if isinstance(raw_offer, _MicrodataNode):
                offer_nodes.append(raw_offer)
        if not offer_nodes:
            offer_nodes = _find_microdata_nodes([prod], {"offer", "aggregateoffer"})

        parsed_offers: List[Tuple[Optional[Decimal], Optional[str], str, Optional[bool], Optional[str]]] = []
        for offer in offer_nodes:
            raw_p = _first_str_prop(offer, ("price", "lowPrice", "highPrice"))
            raw_c = _first_str_prop(offer, ("priceCurrency",))
            raw_a = _first_str_prop(offer, ("availability",))
            o_url = _first_str_prop(offer, ("url",))
            p, c = parse_price_and_currency(raw_p, raw_c)
            av, st = normalize_availability(raw_a)
            parsed_offers.append((p, c, av, st, o_url))

        # Also support price placed directly on Product node
        if not parsed_offers:
            raw_p = _first_str_prop(prod, ("price", "lowPrice"))
            raw_c = _first_str_prop(prod, ("priceCurrency",))
            raw_a = _first_str_prop(prod, ("availability",))
            p, c = parse_price_and_currency(raw_p, raw_c)
            av, st = normalize_availability(raw_a)
            if p is not None or av != "unknown":
                parsed_offers.append((p, c, av, st, prod_url))

        for item in parsed_offers:
            if item[0] is not None and item[3] is True:
                return name, item[1], item[0], item[2], item[3], item[4] or prod_url
        for item in parsed_offers:
            if item[0] is not None:
                return name, item[1], item[0], item[2], item[3], item[4] or prod_url
        if parsed_offers:
            item = parsed_offers[0]
            return name, item[1], item[0], item[2], item[3], item[4] or prod_url

    for offer in offer_nodes_global:
        raw_p = _first_str_prop(offer, ("price", "lowPrice", "highPrice"))
        raw_c = _first_str_prop(offer, ("priceCurrency",))
        raw_a = _first_str_prop(offer, ("availability",))
        o_url = _first_str_prop(offer, ("url",))
        p, c = parse_price_and_currency(raw_p, raw_c)
        av, st = normalize_availability(raw_a)
        if p is not None or av != "unknown":
            return None, c, p, av, st, o_url

    # Flat microdata fallback when itemscope is omitted/broken
    raw_p = next((v for v in flat_props.get("price", []) if _clean_text(v)), None)
    raw_c = next((v for v in flat_props.get("priceCurrency", []) if _clean_text(v)), None)
    raw_a = next((v for v in flat_props.get("availability", []) if _clean_text(v)), None)
    raw_n = next((v for v in flat_props.get("name", []) if _clean_text(v)), None)
    p, c = parse_price_and_currency(raw_p, raw_c)
    av, st = normalize_availability(raw_a)
    if p is not None:
        return _clean_text(raw_n), c, p, av, st, None

    return None


def _extract_from_regex_fallback(
    price_snippets: List[str],
    body_text: str,
) -> Optional[Tuple[Optional[str], Optional[Decimal], str, Optional[bool]]]:
    def _scan_text(txt: str) -> Optional[Tuple[Optional[str], Decimal]]:
        m1 = CURRENCY_BEFORE_PRICE_RE.search(txt)
        m2 = PRICE_BEFORE_CURRENCY_RE.search(txt)
        candidates = []
        if m1:
            candidates.append((m1.start(), m1.group(0)))
        if m2:
            candidates.append((m2.start(), m2.group(0)))
        for _, match_str in sorted(candidates, key=lambda x: x[0]):
            dec, cur = parse_price_and_currency(match_str)
            if dec is not None and cur is not None:
                return cur, dec
        return None

    found_price: Optional[Tuple[Optional[str], Decimal]] = None
    for snippet in price_snippets:
        found_price = _scan_text(snippet)
        if found_price is not None:
            break

    if found_price is None and body_text:
        found_price = _scan_text(body_text)

    if found_price is None:
        return None

    currency, price = found_price
    avail = "unknown"
    in_stock: Optional[bool] = None
    if re.search(r"\b(?:out\s+of\s+stock|sold\s+out|currently\s+unavailable)\b", body_text, re.IGNORECASE):
        avail, in_stock = "out_of_stock", False
    elif re.search(r"\bpre[-\s]?order\b", body_text, re.IGNORECASE):
        avail, in_stock = "preorder", False
    elif re.search(r"\bin\s+stock\b", body_text, re.IGNORECASE):
        avail, in_stock = "in_stock", True

    return currency, price, avail, in_stock


def extract_from_html(
    html_content: str,
    source: str = "<html-string>",
    default_url: Optional[str] = None,
) -> ExtractionResult:
    """Extract product price, currency, availability, and metadata from raw HTML."""
    parser = _ProductHTMLParser()
    parser.feed(html_content)
    parser.close()

    og_title = None
    for attrs in parser.meta_tags:
        key = (attrs.get("property") or attrs.get("name") or "").strip().lower()
        if key in {"og:title", "twitter:title"} and attrs.get("content"):
            og_title = _clean_text(attrs["content"])
            break

    fallback_name = og_title or parser.h1_text or parser.title_text
    fallback_url = parser.canonical_url or default_url or (
        source if source.startswith(("http://", "https://")) else None
    )

    jsonld_res = _extract_from_jsonld(parser.jsonld_blocks)
    og_res = _extract_from_opengraph(parser.meta_tags)
    micro_res = _extract_from_microdata(parser.microdata_roots, parser.flat_microdata_props)

    # Tier 1: JSON-LD with valid price
    if jsonld_res is not None and jsonld_res[2] is not None:
        name, curr, price, avail, in_stock, url = jsonld_res
        return ExtractionResult(
            source=source,
            name=name or fallback_name,
            currency=curr,
            price=price,
            availability=avail,
            in_stock=in_stock,
            extractor_source="json-ld",
            url=url or fallback_url,
        )

    # Tier 2: OpenGraph with valid price
    if og_res is not None and og_res[2] is not None:
        name, curr, price, avail, in_stock, url = og_res
        return ExtractionResult(
            source=source,
            name=name or (jsonld_res[0] if jsonld_res else None) or fallback_name,
            currency=curr,
            price=price,
            availability=avail,
            in_stock=in_stock,
            extractor_source="opengraph",
            url=url or fallback_url,
        )

    # Tier 3: Microdata with valid price
    if micro_res is not None and micro_res[2] is not None:
        name, curr, price, avail, in_stock, url = micro_res
        return ExtractionResult(
            source=source,
            name=name or (jsonld_res[0] if jsonld_res else None) or fallback_name,
            currency=curr,
            price=price,
            availability=avail,
            in_stock=in_stock,
            extractor_source="microdata",
            url=url or fallback_url,
        )

    # Tier 4: Fallback regex on visible price elements / body text
    body_text = _clean_text(" ".join(parser.body_text_parts)) or ""
    regex_res = _extract_from_regex_fallback(parser.price_hint_snippets, body_text)
    if regex_res is not None:
        curr, price, avail, in_stock = regex_res
        best_name = (
            (jsonld_res[0] if jsonld_res else None)
            or (og_res[0] if og_res else None)
            or (micro_res[0] if micro_res else None)
            or fallback_name
        )
        return ExtractionResult(
            source=source,
            name=best_name,
            currency=curr,
            price=price,
            availability=avail,
            in_stock=in_stock,
            extractor_source="regex-fallback",
            url=fallback_url,
        )

    # Graceful partial structured result when availability/name exists without price
    for tier_name, tier_res in (("json-ld", jsonld_res), ("opengraph", og_res), ("microdata", micro_res)):
        if tier_res is not None:
            name, curr, price, avail, in_stock, url = tier_res
            return ExtractionResult(
                source=source,
                name=name or fallback_name,
                currency=curr,
                price=price,
                availability=avail,
                in_stock=in_stock,
                extractor_source=tier_name,
                url=url or fallback_url,
            )

    return ExtractionResult(
        source=source,
        name=fallback_name,
        url=fallback_url,
    )


def extract_from_file(file_path: Union[str, Path]) -> ExtractionResult:
    """Read an HTML file from disk and extract product pricing metadata."""
    path = Path(file_path)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ExtractionResult(source=str(path), error=f"File read error: {exc}")
    return extract_from_html(raw, source=str(path))


def _decode_http_bytes(raw_bytes: bytes, content_type: str, content_encoding: str) -> str:
    data = raw_bytes
    if "gzip" in content_encoding.lower() or data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except OSError:
            pass

    charset = "utf-8"
    m_ct = re.search(r"charset=([^\s;\"']+)", content_type, re.IGNORECASE)
    if m_ct:
        charset = m_ct.group(1).strip()
    else:
        head = data[:4096].decode("ascii", errors="ignore")
        m_meta = re.search(r"<meta[^>]+charset=[\"']?([A-Za-z0-9_\-]+)", head, re.IGNORECASE)
        if m_meta:
            charset = m_meta.group(1).strip()

    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def extract_from_url(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
) -> ExtractionResult:
    """Fetch a remote URL (HTTP/HTTPS) and extract product pricing metadata."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_bytes = resp.read()
            headers = resp.headers
            content_type = headers.get("Content-Type", "") if headers else ""
            content_encoding = headers.get("Content-Encoding", "") if headers else ""
            final_url = resp.geturl() if hasattr(resp, "geturl") else url
    except urllib.error.HTTPError as exc:
        return ExtractionResult(source=url, url=url, error=f"HTTP {exc.code}: {exc.reason}")
    except urllib.error.URLError as exc:
        return ExtractionResult(source=url, url=url, error=f"URL error: {exc.reason}")
    except Exception as exc:
        return ExtractionResult(source=url, url=url, error=f"Fetch error: {exc}")

    html_text = _decode_http_bytes(raw_bytes, content_type, content_encoding)
    result = extract_from_html(html_text, source=url, default_url=final_url)
    return result


def extract_from_target(
    target: str,
    timeout: float = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
) -> ExtractionResult:
    """Extract product pricing metadata from a URL, file:// URI, or local HTML file path."""
    cleaned = target.strip()
    if cleaned.startswith(("http://", "https://")):
        return extract_from_url(cleaned, timeout=timeout, user_agent=user_agent)
    if cleaned.startswith("file://"):
        local_path = urllib.request.url2pathname(cleaned[len("file://") :])
        return extract_from_file(local_path)
    return extract_from_file(cleaned)
