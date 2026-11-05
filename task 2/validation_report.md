# Data Validation Report

- **Generated:** 2026-11-04T14:40:49+00:00
- **Input:** `messy_sample.csv`
- **Verdict:** **PASS WITH WARNINGS**
- **Settings:** target currency `USD`, default currency `GBP`, dayfirst=True

## 1. Row flow

| Stage | Rows |
|---|---|
| Raw input | 138 |
| Quarantined (failed validation) | -14 |
| Exact duplicates removed | -6 |
| Same-product duplicates removed (URL key) | -12 |
| **Clean output** | **106** |

Retention: **76.81%** of raw rows.

## 2. Cleaning actions

| Action | Count |
|---|---|
| Prices unparseable (set to null) | 1 |
| Prices with unsupported currency | 1 |
| Dates unparseable (set to null) | 1 |
| Ratings unparseable (set to null) | 0 |
| `in_stock` derived from availability text | 19 |
| `availability` null -> 'Unknown' | 2 |

## 3. Currency normalization

| Original currency | Records |
|---|---|
| CHF | 1 |
| EUR | 13 |
| GBP | 62 |
| INR | 22 |
| USD | 39 |

All prices converted to **USD**; originals kept in `price_original` / `currency_original`. FX rates are a static table.

## 4. Null profile (% null, before vs. after)

| Column | Raw nulls | Clean nulls |
|---|---|---|
| title | 2 (1.45%) | 0 (0.0%) |
| price | 0 (0.0%) | 0 (0.0%) |
| currency | 138 (100.0%) | 0 (0.0%) |
| price_original | n/a (n/a%) | 0 (0.0%) |
| currency_original | n/a (n/a%) | 0 (0.0%) |
| rating | 0 (0.0%) | 0 (0.0%) |
| in_stock | 19 (13.77%) | 0 (0.0%) |
| availability | 2 (1.45%) | 0 (0.0%) |
| product_url | 1 (0.72%) | 0 (0.0%) |
| image_url | 0 (0.0%) | 0 (0.0%) |
| page | 0 (0.0%) | 0 (0.0%) |
| scraped_at | 0 (0.0%) | 0 (0.0%) |

## 5. Rule violations (rows quarantined)

| Rule | Rows |
|---|---|
| missing_or_invalid:title | 2 |
| missing_or_invalid:price | 2 |
| below_min:price(<=0) | 3 |
| missing_or_invalid:price_original | 1 |
| missing_or_invalid:currency_original | 1 |
| above_max:rating(>5) | 2 |
| missing_or_invalid:product_url | 1 |
| bad_format:product_url | 1 |
| missing_or_invalid:scraped_at | 1 |
| future_date:scraped_at | 2 |
| unsupported_currency | 1 |

A row may break several rules. Rejected rows are in `quarantine.csv` with reasons.

## 6. Schema enforcement

| Column | Expected | Actual dtype | Nulls | Required | Status |
|---|---|---|---|---|---|
| title | str | string | 0 | True | PASS |
| price | float | float64 | 0 | True | PASS |
| currency | str | string | 0 | True | PASS |
| price_original | float | float64 | 0 | True | PASS |
| currency_original | str | string | 0 | True | PASS |
| rating | int | Int64 | 0 | False | PASS |
| in_stock | bool | boolean | 0 | False | PASS |
| availability | str | string | 0 | False | PASS |
| product_url | str | string | 0 | True | PASS |
| image_url | str | string | 0 | False | PASS |
| page | int | Int64 | 0 | False | PASS |
| scraped_at | datetime | datetime64[us, UTC] | 0 | True | PASS |

## 7. Warnings

- 14 rows quarantined; review quarantine.csv.
- Retention 76.81% is below the 80% threshold.
