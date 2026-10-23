#!/usr/bin/env python3
"""
Extract data/bins.csv, data/items.csv, data/stock.csv for wms_sim.py from the
CKB WMS repository's opening balance (supabase/seed.sql = WMS sheet 24 Sep 2026).

Usage:  python extract_data.py [path/to/seed.sql]
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

DEFAULT_SEED = Path.home() / "allocator_k-one" / "supabase" / "seed.sql"
OUT = Path(__file__).resolve().parent / "data"

BIN_COLS = ("bin_code", "zone", "rack", "level", "position", "status", "capacity")
ITEM_COLS = ("sku", "description", "uom", "upp", "volume_l", "abc_class")
STOCK_COLS = ("bin", "sku", "batch", "qty", "exp", "gr")


def tuples_after(sql: str, marker: str) -> list[str]:
    """Return the raw single-line tuple bodies after `marker` (until on-conflict/closing ")."."""
    start = sql.index(marker)
    body = sql[start:]
    end = min(x for x in (body.find("on conflict"), body.find(") as v(")) if x != -1)
    rows = []
    for line in body[:end].splitlines()[1:]:
        s = line.strip()
        if not (
            s.startswith("(")
            and (s.endswith(",") or s.endswith(";") or s.endswith(")"))
        ):
            continue
        k = s[-1]
        inner = s[1:-1]
        if inner.endswith(")"):
            inner = inner[:-1]
        rows.append(inner)
    return rows


def parse_row(raw: str) -> list[str]:
    """'a','b',null,'it''s',2 -> ['a','b',None,"it's",'2']  (SQL-style escapes and bare words)."""
    parser = csv.reader(
        io.StringIO(raw), quotechar="'", doublequote=True, skipinitialspace=True
    )
    vals = next(parser)
    return [None if v == "null" or v == "" else v for v in vals]


def write_csv(path: Path, header: list[str], rows: list[list]) -> int:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return len(rows)


def main() -> None:
    seed_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SEED
    sql = seed_path.read_text(encoding="utf-8")

    OUT.mkdir(parents=True, exist_ok=True)

    bins_raw = tuples_after(
        sql,
        "insert into public.bins (bin_code, zone, rack, level, position, status, capacity) values",
    )
    bins = [parse_row(r) for r in bins_raw]
    n_bins = write_csv(OUT / "bins.csv", list(BIN_COLS), bins)

    items_raw = tuples_after(
        sql,
        "insert into public.items (sku, description, uom, upp, volume_l, abc_class) values",
    )
    items = [parse_row(r) for r in items_raw]
    n_items = write_csv(OUT / "items.csv", list(ITEM_COLS), items)

    stock_raw = tuples_after(sql, "from (values")
    stock = []
    for r in stock_raw:
        v = parse_row(r)
        if v[0] == "bin":  # header echo, not data
            continue
        if v[0] in ("QUARANTINE", "STAGING", "STAGING_INB", "STAGING_OUT") or v[
            0
        ].startswith("STG_"):
            stock.append([v[0], v[1], "", int(v[3]), "", ""])
        else:
            stock.append([v[0], v[1], v[2] or "", int(v[3]), v[4] or "", v[5] or ""])
    n_stock = write_csv(
        OUT / "stock.csv", ["bin_code", "sku", "batch", "qty", "expiry", "gr"], stock
    )

    print(f"bins.csv        {n_bins} rows  (expect 2570)")
    print(f"items.csv       {n_items} rows  (expect 106)")
    print(f"stock.csv       {n_stock} rows  (expect ~1790)")


if __name__ == "__main__":
    main()
