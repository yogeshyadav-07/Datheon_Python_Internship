#!/usr/bin/env python3
"""
Automated data-cleaning pipeline: nulls -> currency -> dates -> validation -> dedup
-> strict schema enforcement, with an auto-generated data-quality report.

Designed for the output of scraper.py (title, price, rating, in_stock, ...), but the
SCHEMA / rate table below are the only things you change for another dataset.

Usage:
    pip install pandas
    python clean_data.py --input books_dataset.csv
    python clean_data.py --input raw.csv --target-currency INR --default-currency GBP --dayfirst

Outputs (in --outdir):
    clean_dataset.csv / .json     validated, deduplicated, typed records
    quarantine.csv                rejected rows + the reason(s) they failed
    validation_report.md / .json  human + machine readable quality report
    cleaning.log                  step-by-step execution log
"""

import argparse
import html
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("cleaner")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
NULL_TOKENS = {"", "n/a", "na", "nan", "null", "none", "nil", "-", "--", "?", "missing", "unknown"}

COLUMN_ALIASES = {
    "name": "title", "product": "title", "product_name": "title",
    "url": "product_url", "link": "product_url",
    "cost": "price", "amount": "price",
    "date": "scraped_at", "timestamp": "scraped_at", "date_scraped": "scraped_at",
    "stock": "in_stock", "image": "image_url",
}

# Illustrative static FX table: units of USD per 1 unit of currency.
# Replace with live/historical rates for production use (or pass --rates rates.json).
RATES_TO_USD = {"USD": 1.0, "GBP": 1.27, "EUR": 1.08, "INR": 0.012, "JPY": 0.0067, "CAD": 0.73, "AUD": 0.66}

# Longest symbols first so "Rs." wins over "Rs".
CURRENCY_SYMBOLS = [("Rs.", "INR"), ("Rs", "INR"), ("£", "GBP"), ("€", "EUR"),
                    ("₹", "INR"), ("¥", "JPY"), ("$", "USD")]

RATING_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
TRUE_TOKENS = {"true", "t", "yes", "y", "1", "in stock", "available"}
FALSE_TOKENS = {"false", "f", "no", "n", "0", "out of stock", "unavailable", "sold out"}

URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE)

# Strict target schema. kind: str | float | int | bool | datetime
SCHEMA = {
    "title":             {"kind": "str",      "required": True},
    "price":             {"kind": "float",    "required": True, "min": 0, "min_exclusive": True, "max": 10_000_000},
    "currency":          {"kind": "str",      "required": True},
    "price_original":    {"kind": "float",    "required": True},
    "currency_original": {"kind": "str",      "required": True},
    "rating":            {"kind": "int",      "required": False, "min": 1, "max": 5},
    "in_stock":          {"kind": "bool",     "required": False},
    "availability":      {"kind": "str",      "required": False},
    "product_url":       {"kind": "str",      "required": True, "regex": URL_RE, "unique": True},
    "image_url":         {"kind": "str",      "required": False},
    "page":              {"kind": "int",      "required": False, "min": 1},
    "scraped_at":        {"kind": "datetime", "required": True, "min_date": "2000-01-01"},
}


# --------------------------------------------------------------------------- #
# Field-level parsers
# --------------------------------------------------------------------------- #
def parse_money(raw, fallback_ccy: str):
    """'£1,299.50' / '51,77 €' / 'USD 20' / '(5.00)' -> (amount, currency_code).
    Returns (nan, None) when unparseable. Currency priority: symbol/code in the
    string, then the fallback (a 'currency' column or --default-currency)."""
    if pd.isna(raw):
        return np.nan, None
    s = str(raw).replace("Â", "").strip()  # repair common mojibake ('Â£')
    ccy = None
    for sym, code in CURRENCY_SYMBOLS:
        if sym in s:
            ccy, s = code, s.replace(sym, "")
            break
    code_match = re.search(r"[A-Za-z]{3}", s)
    if code_match:
        ccy, s = code_match.group(0).upper(), s.replace(code_match.group(0), "")
    s = s.strip()
    negative = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    s = re.sub(r"[()\-\s']", "", s)
    if not re.fullmatch(r"[\d.,]+", s or "x"):
        return np.nan, ccy or fallback_ccy
    if "," in s and "." in s:  # whichever comes last is the decimal separator
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in s:
        s = s.replace(",", ".") if re.search(r",\d{1,2}$", s) else s.replace(",", "")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        value = float(s)
    except ValueError:
        return np.nan, ccy or fallback_ccy
    return (-value if negative else value), (ccy or fallback_ccy)


def make_date_parser(dayfirst: bool):
    slash = ["%d/%m/%Y", "%m/%d/%Y"] if dayfirst else ["%m/%d/%Y", "%d/%m/%Y"]
    dash = ["%d-%m-%Y", "%m-%d-%Y"] if dayfirst else ["%m-%d-%Y", "%d-%m-%Y"]
    formats = (["%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"] + slash + dash +
               ["%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"])

    def parse(raw):
        if pd.isna(raw):
            return pd.NaT
        s = str(raw).strip()
        dt = None
        try:  # ISO 8601 (with offset / 'Z') first
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            for fmt in formats:
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            return pd.NaT
        dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        return pd.Timestamp(dt)

    return parse


def parse_rating(v):
    if pd.isna(v):
        return np.nan
    s = str(v).strip().lower()
    if s in RATING_WORDS:
        return RATING_WORDS[s]
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:/\s*5)?", s)
    return float(m.group(1)) if m else np.nan


def parse_bool(v):
    if pd.isna(v):
        return pd.NA
    s = str(v).strip().lower()
    return True if s in TRUE_TOKENS else False if s in FALSE_TOKENS else pd.NA


def null_profile(df: pd.DataFrame) -> dict:
    n = len(df)
    return {c: {"nulls": int(df[c].isna().sum()),
                "pct": round(100 * df[c].isna().sum() / n, 2) if n else 0.0} for c in df.columns}


# --------------------------------------------------------------------------- #
# Pipeline stages
# --------------------------------------------------------------------------- #
def load_and_standardize(path: Path, stats: dict) -> pd.DataFrame:
    # Read everything as text and disable pandas' own NA guessing so null handling is explicit.
    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    stats["raw_rows"], stats["raw_columns"] = len(df), list(df.columns)
    df.columns = [re.sub(r"\W+", "_", c.strip().lower()).strip("_") for c in df.columns]
    df = df.rename(columns={k: v for k, v in COLUMN_ALIASES.items() if k in df.columns and v not in df.columns})
    df = df.loc[:, ~df.columns.duplicated()]
    for col in SCHEMA:  # ensure every schema column exists
        if col not in df.columns and col not in ("currency", "price_original", "currency_original"):
            df[col] = ""
            log.warning("Column '%s' missing from input; created empty", col)
    return df


def normalize_nulls_and_text(df: pd.DataFrame, stats: dict) -> pd.DataFrame:
    df = df.astype("string")
    for col in df.columns:
        s = df[col].str.replace(r"\s+", " ", regex=True).str.strip()
        df[col] = s.where(~s.str.lower().isin(NULL_TOKENS), pd.NA)
    df["title"] = df["title"].map(lambda v: html.unescape(v) if pd.notna(v) else v)
    stats["null_profile_raw"] = null_profile(df)
    log.info("Normalized null tokens and whitespace; %d cells are null",
             int(df.isna().sum().sum()))
    return df


def clean_prices(df: pd.DataFrame, target: str, default_ccy: str, rates: dict, stats: dict) -> pd.DataFrame:
    fallback = df["currency"].str.upper() if "currency" in df.columns else pd.Series(default_ccy, index=df.index)
    fallback = fallback.where(fallback.isin(rates), default_ccy)
    parsed = [parse_money(p, f) for p, f in zip(df["price"], fallback)]
    amount = pd.Series([p[0] for p in parsed], index=df.index, dtype="float64")
    ccy = pd.Series([p[1] for p in parsed], index=df.index, dtype="string")

    raw_present = df["price"].notna()
    stats["price_unparseable"] = int((raw_present & amount.isna()).sum())
    stats["currency_counts"] = ccy[amount.notna()].value_counts().to_dict()
    unsupported = amount.notna() & ~ccy.isin(rates)
    stats["currency_unsupported"] = int(unsupported.sum())

    usd = amount * ccy.map(rates).astype("float64")
    df["price_original"], df["currency_original"] = amount, ccy
    df["price"] = (usd / rates[target]).round(2)  # NaN if currency unsupported
    df["currency"] = target
    df.loc[amount.isna(), "currency_original"] = pd.NA
    log.info("Prices: %d parsed, %d unparseable, %d unsupported currency; normalized to %s",
             int(amount.notna().sum()), stats["price_unparseable"], stats["currency_unsupported"], target)
    return df


def clean_dates(df: pd.DataFrame, dayfirst: bool, stats: dict) -> pd.DataFrame:
    parse = make_date_parser(dayfirst)
    parsed = pd.to_datetime(df["scraped_at"].map(parse), utc=True)
    stats["date_unparseable"] = int((df["scraped_at"].notna() & parsed.isna()).sum())
    df["scraped_at"] = parsed
    log.info("Dates: %d parsed to UTC, %d unparseable (dayfirst=%s)",
             int(parsed.notna().sum()), stats["date_unparseable"], dayfirst)
    return df


def clean_misc(df: pd.DataFrame, stats: dict) -> pd.DataFrame:
    rating = df["rating"].map(parse_rating)
    stats["rating_unparseable"] = int((df["rating"].notna() & rating.isna()).sum())
    df["rating"] = pd.to_numeric(rating, errors="coerce").round().astype("Int64")

    explicit = df["in_stock"].map(parse_bool).astype("boolean")
    derived = df["availability"].str.lower().str.contains("in stock", na=False)
    derived = derived.where(df["availability"].notna(), pd.NA).astype("boolean")
    stats["in_stock_derived"] = int((explicit.isna() & derived.notna()).sum())
    df["in_stock"] = explicit.fillna(derived)

    stats["availability_filled"] = int(df["availability"].isna().sum())
    df["availability"] = df["availability"].fillna("Unknown")  # optional text field -> sentinel
    df["page"] = pd.to_numeric(df["page"], errors="coerce").astype("Int64")
    df["product_url"] = df["product_url"].str.strip()
    return df


def validate(df: pd.DataFrame, stats: dict):
    """Row-level rules. Returns (valid_df, quarantine_df). Failing rows are never silently dropped."""
    reasons = pd.Series([[] for _ in range(len(df))], index=df.index)
    rule_counts = {}

    def flag(name, mask):
        mask = mask.fillna(False).astype(bool)
        rule_counts[name] = int(mask.sum())
        for idx in df.index[mask]:
            reasons[idx].append(name)

    for col, spec in SCHEMA.items():
        if spec["required"]:
            flag(f"missing_or_invalid:{col}", df[col].isna())
        if "min" in spec:
            bad = (df[col] <= spec["min"]) if spec.get("min_exclusive") else (df[col] < spec["min"])
            flag(f"below_min:{col}(<{'=' if spec.get('min_exclusive') else ''}{spec['min']})", bad)
        if "max" in spec:
            flag(f"above_max:{col}(>{spec['max']})", df[col] > spec["max"])
        if "regex" in spec:
            flag(f"bad_format:{col}", df[col].notna() & ~df[col].astype("string").str.match(spec["regex"], na=False))
    flag("future_date:scraped_at", df["scraped_at"] > pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=1))
    flag("too_old:scraped_at", df["scraped_at"] < pd.Timestamp(SCHEMA["scraped_at"]["min_date"], tz="UTC"))
    flag("unsupported_currency", df["currency_original"].notna() & df["price"].isna() & df["price_original"].notna())

    stats["rule_violations"] = {k: v for k, v in rule_counts.items() if v}
    failed = reasons.map(len) > 0
    quarantine = df[failed].copy()
    quarantine["rejection_reasons"] = reasons[failed].map("; ".join)
    log.info("Validation: %d valid, %d quarantined", int((~failed).sum()), int(failed.sum()))
    return df[~failed].copy(), quarantine


def deduplicate(df: pd.DataFrame, stats: dict) -> pd.DataFrame:
    before = len(df)
    df = df.drop_duplicates()
    stats["duplicates_exact"] = before - len(df)

    df["_key"] = df["product_url"].str.lower().str.rstrip("/")
    df["_completeness"] = df.notna().sum(axis=1)
    # Keep the most recently scraped, most complete version of each product.
    df = (df.sort_values(["scraped_at", "_completeness"], ascending=False, na_position="last")
            .drop_duplicates("_key", keep="first").sort_index())
    stats["duplicates_by_key"] = before - stats["duplicates_exact"] - len(df)
    log.info("Dedup: removed %d exact and %d same-product (URL key) duplicates",
             stats["duplicates_exact"], stats["duplicates_by_key"])
    return df.drop(columns=["_key", "_completeness"])


def enforce_schema(df: pd.DataFrame, stats: dict) -> pd.DataFrame:
    dtype_map = {"str": "string", "float": "float64", "int": "Int64", "bool": "boolean"}
    df = df[list(SCHEMA)].copy()
    for col, spec in SCHEMA.items():
        if spec["kind"] != "datetime":
            df[col] = df[col].astype(dtype_map[spec["kind"]])

    checks = []
    for col, spec in SCHEMA.items():
        dt = df[col].dtype
        ok = {"str": pd.api.types.is_string_dtype(dt), "float": pd.api.types.is_float_dtype(dt),
              "int": isinstance(dt, pd.Int64Dtype), "bool": isinstance(dt, pd.BooleanDtype),
              "datetime": isinstance(dt, pd.DatetimeTZDtype)}[spec["kind"]]
        checks.append({"column": col, "expected": spec["kind"], "actual": str(dt), "ok": bool(ok),
                       "nulls": int(df[col].isna().sum()), "required": spec["required"]})
        if spec["required"] and df[col].isna().any():
            checks[-1]["ok"] = False
        if spec.get("unique") and df[col].duplicated().any():
            checks[-1]["ok"] = False
    stats["schema_checks"] = checks
    return df


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def build_report(s: dict, args) -> str:
    verdict = s["verdict"]
    L = [f"# Data Validation Report", "",
         f"- **Generated:** {s['generated_at']}", f"- **Input:** `{args.input}`",
         f"- **Verdict:** **{verdict}**",
         f"- **Settings:** target currency `{args.target_currency}`, default currency "
         f"`{args.default_currency}`, dayfirst={args.dayfirst}", "",
         "## 1. Row flow", "",
         md_table(["Stage", "Rows"], [
             ["Raw input", s["raw_rows"]],
             ["Quarantined (failed validation)", f"-{s['quarantined_rows']}"],
             ["Exact duplicates removed", f"-{s['duplicates_exact']}"],
             ["Same-product duplicates removed (URL key)", f"-{s['duplicates_by_key']}"],
             ["**Clean output**", f"**{s['clean_rows']}**"]]),
         f"\nRetention: **{s['retention_pct']}%** of raw rows.", "",
         "## 2. Cleaning actions", "",
         md_table(["Action", "Count"], [
             ["Prices unparseable (set to null)", s["price_unparseable"]],
             ["Prices with unsupported currency", s["currency_unsupported"]],
             ["Dates unparseable (set to null)", s["date_unparseable"]],
             ["Ratings unparseable (set to null)", s["rating_unparseable"]],
             ["`in_stock` derived from availability text", s["in_stock_derived"]],
             ["`availability` null -> 'Unknown'", s["availability_filled"]]]), "",
         "## 3. Currency normalization", "",
         md_table(["Original currency", "Records"],
                  [[k, v] for k, v in sorted(s["currency_counts"].items())] or [["-", 0]]),
         f"\nAll prices converted to **{args.target_currency}**; originals kept in "
         "`price_original` / `currency_original`. FX rates are a static table.", "",
         "## 4. Null profile (% null, before vs. after)", ""]
    rows = []
    for col in SCHEMA:
        b = s["null_profile_raw"].get(col, {"nulls": "n/a", "pct": "n/a"})
        a = s["null_profile_clean"][col]
        rows.append([col, f"{b['nulls']} ({b['pct']}%)", f"{a['nulls']} ({a['pct']}%)"])
    L += [md_table(["Column", "Raw nulls", "Clean nulls"], rows), "",
          "## 5. Rule violations (rows quarantined)", ""]
    viol = s["rule_violations"]
    L += [md_table(["Rule", "Rows"], [[k, v] for k, v in viol.items()]) if viol else "_No violations._", "",
          "A row may break several rules. Rejected rows are in `quarantine.csv` with reasons.", "",
          "## 6. Schema enforcement", "",
          md_table(["Column", "Expected", "Actual dtype", "Nulls", "Required", "Status"],
                   [[c["column"], c["expected"], c["actual"], c["nulls"], c["required"],
                     "PASS" if c["ok"] else "FAIL"] for c in s["schema_checks"]]), ""]
    if s["warnings"]:
        L += ["## 7. Warnings", ""] + [f"- {w}" for w in s["warnings"]] + [""]
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(args) -> int:
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(), logging.FileHandler(outdir / "cleaning.log", "w", "utf-8")])

    rates = dict(RATES_TO_USD)
    if args.rates:
        rates.update(json.loads(Path(args.rates).read_text()))
    for ccy in (args.target_currency, args.default_currency):
        if ccy not in rates:
            log.error("Currency %s not in rate table", ccy)
            return 2

    stats = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "warnings": []}
    df = load_and_standardize(Path(args.input), stats)
    df = normalize_nulls_and_text(df, stats)
    df = clean_prices(df, args.target_currency, args.default_currency, rates, stats)
    df = clean_dates(df, args.dayfirst, stats)
    df = clean_misc(df, stats)

    valid, quarantine = validate(df, stats)
    stats["quarantined_rows"] = len(quarantine)
    clean = deduplicate(valid, stats)
    clean = enforce_schema(clean, stats)

    stats["clean_rows"] = len(clean)
    stats["retention_pct"] = round(100 * len(clean) / stats["raw_rows"], 2) if stats["raw_rows"] else 0
    stats["null_profile_clean"] = null_profile(clean)
    if stats["quarantined_rows"]:
        stats["warnings"].append(f"{stats['quarantined_rows']} rows quarantined; review quarantine.csv.")
    if stats["retention_pct"] < 80:
        stats["warnings"].append(f"Retention {stats['retention_pct']}% is below the 80% threshold.")
    failed_cols = [c["column"] for c in stats["schema_checks"] if not c["ok"]]
    stats["verdict"] = ("FAIL" if failed_cols or clean.empty
                        else "PASS WITH WARNINGS" if stats["warnings"] else "PASS")
    if failed_cols:
        stats["warnings"].append(f"Schema check failed for: {', '.join(failed_cols)}")

    export = clean.copy()
    export["scraped_at"] = export["scraped_at"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    export.to_csv(outdir / "clean_dataset.csv", index=False, encoding="utf-8-sig")
    (outdir / "clean_dataset.json").write_text(
        export.astype(object).where(export.notna(), None).to_json(orient="records", indent=2, force_ascii=False),
        encoding="utf-8")
    quarantine.drop(columns=[c for c in quarantine.columns if c.startswith("_")]).to_csv(
        outdir / "quarantine.csv", index=False, encoding="utf-8-sig")
    (outdir / "validation_report.json").write_text(json.dumps(stats, indent=2, default=str), encoding="utf-8")
    (outdir / "validation_report.md").write_text(build_report(stats, args), encoding="utf-8")
    log.info("Verdict: %s | %d -> %d rows | outputs in %s/", stats["verdict"],
             stats["raw_rows"], stats["clean_rows"], outdir)
    return 1 if stats["verdict"] == "FAIL" else 0


def main():
    ap = argparse.ArgumentParser(description="Automated data cleaning + validation")
    ap.add_argument("--input", required=True, help="raw CSV (e.g. scraper.py output)")
    ap.add_argument("--outdir", default="clean_output")
    ap.add_argument("--target-currency", default="USD", help="currency to normalize all prices to")
    ap.add_argument("--default-currency", default="USD", help="used when a price has no symbol/code")
    ap.add_argument("--dayfirst", action="store_true", help="read 04/11/2026 as 4 Nov (default: 11 Apr)")
    ap.add_argument("--rates", help="JSON file of {CODE: usd_per_unit} to extend/override FX table")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
