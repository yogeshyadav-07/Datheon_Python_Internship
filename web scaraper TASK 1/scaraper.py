#!/usr/bin/env python3
"""
Paginated web scraper: requests + BeautifulSoup -> CSV / JSON.

Default target is https://books.toscrape.com (a sandbox site built for scraping
practice: 1000 products across 50 pages). Pass --max-pages to stop early.

Usage:
    pip install requests beautifulsoup4
    python scraper.py                       # scrape all pages
    python scraper.py --max-pages 6         # ~120 records
    python scraper.py --out books --delay 1 # custom output prefix / politeness delay
"""

import argparse
import csv
import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://books.toscrape.com/catalogue/page-1.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

RATING_MAP = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}
FIELDS = [
    "title", "price", "currency", "rating", "in_stock",
    "availability", "product_url", "image_url", "page", "scraped_at",
]

log = logging.getLogger("scraper")


# --------------------------------------------------------------------------- #
# HTTP layer: session with connection-level retries + manual retry w/ backoff
# --------------------------------------------------------------------------- #
def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def fetch(session: requests.Session, url: str, retries: int = 3, timeout: int = 15):
    """GET a URL with exponential backoff. Returns Response or None on failure."""
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code == 404:
                log.info("404 for %s (treating as end of pagination)", url)
                return None
            resp.raise_for_status()
            resp.encoding = "utf-8"  # avoids 'Â£' mojibake on price strings
            return resp
        except requests.RequestException as exc:
            wait = 2 ** attempt
            log.warning("Attempt %d/%d failed for %s: %s", attempt, retries, url, exc)
            if attempt < retries:
                time.sleep(wait)
    log.error("Giving up on %s", url)
    return None


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_price(text: str):
    """'£51.77' -> (51.77, '£'). Returns (None, '') if no number found."""
    match = re.search(r"(\d[\d,]*\.?\d*)", text or "")
    currency = re.sub(r"[\d.,\s]", "", text or "")
    if not match:
        return None, currency
    return float(match.group(1).replace(",", "")), currency


def parse_product(card, page_url: str, page_no: int) -> dict | None:
    """Extract one record from a product <article>. Returns None if malformed."""
    try:
        link = card.select_one("h3 a")
        title = (link.get("title") or link.get_text(strip=True)).strip()
        price, currency = parse_price(card.select_one("p.price_color").get_text())

        rating_tag = card.select_one("p.star-rating")
        rating_word = next(
            (c for c in rating_tag.get("class", []) if c in RATING_MAP), None
        )

        avail_tag = card.select_one("p.instock.availability")
        availability = avail_tag.get_text(strip=True) if avail_tag else ""

        img = card.select_one("img")
        return {
            "title": title,
            "price": price,
            "currency": currency,
            "rating": RATING_MAP.get(rating_word),
            "in_stock": "in stock" in availability.lower(),
            "availability": availability,
            "product_url": urljoin(page_url, link["href"]),
            "image_url": urljoin(page_url, img["src"]) if img else "",
            "page": page_no,
            "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    except (AttributeError, KeyError, TypeError) as exc:
        log.warning("Skipping malformed product card on page %d: %s", page_no, exc)
        return None


def parse_page(html: str, page_url: str, page_no: int):
    """Return (records, next_page_url or None)."""
    soup = BeautifulSoup(html, "html.parser")
    records = [
        rec
        for card in soup.select("article.product_pod")
        if (rec := parse_product(card, page_url, page_no))
    ]
    next_link = soup.select_one("li.next a")
    next_url = urljoin(page_url, next_link["href"]) if next_link else None
    return records, next_url


# --------------------------------------------------------------------------- #
# Crawl loop + export
# --------------------------------------------------------------------------- #
def scrape(start_url: str, max_pages: int | None, delay: float) -> list[dict]:
    session = build_session()
    seen, results = set(), []
    url, page_no = start_url, 1

    while url and (max_pages is None or page_no <= max_pages):
        log.info("Page %d: %s", page_no, url)
        resp = fetch(session, url)
        if resp is None:
            break

        records, next_url = parse_page(resp.text, url, page_no)
        new = [r for r in records if r["product_url"] not in seen]  # de-duplicate
        seen.update(r["product_url"] for r in new)
        results.extend(new)
        log.info("  +%d records (total %d)", len(new), len(results))

        url, page_no = next_url, page_no + 1
        time.sleep(delay)  # be polite to the server

    return results


def export(records: list[dict], prefix: str) -> None:
    with open(f"{prefix}.json", "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    with open(f"{prefix}.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(records)
    log.info("Wrote %d records to %s.json and %s.csv", len(records), prefix, prefix)


def main():
    ap = argparse.ArgumentParser(description="Paginated product scraper")
    ap.add_argument("--url", default=BASE_URL, help="first listing page URL")
    ap.add_argument("--max-pages", type=int, default=None, help="stop after N pages")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds between requests")
    ap.add_argument("--out", default="books_dataset", help="output file prefix")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    records = scrape(args.url, args.max_pages, args.delay)
    if not records:
        log.error("No records scraped; nothing to export.")
        raise SystemExit(1)
    export(records, args.out)


if __name__ == "__main__":
    main()