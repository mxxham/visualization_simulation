#!/usr/bin/env python3
"""
extract_schedule.py - digest the "Schedule of the day" sheet of a WMS xlsx
into data/schedule.json so wms_sim.py can simulate the actual orders.

    .venv/bin/python extract_schedule.py [xlsx] [--day-out data/schedule.json]

The sheet is the outbound plan: one row per order line (material x shipment).
A Shipment Number identifies one truck/delivery with:
    - Arrival Slot Time  (planned clock time, e.g. 08:00)
    - Type Truck         (WBX / CDD / CDE / TRAGA / C20 / GRM ...)
    - Destination + city, Order No, Purchase Order Number, First Delivery Date
    - lines: Material, Delivery quantity (CAR = cartons, EA = drums/pails)
Line items whose material is not in data/items.csv (newer SKUs than the seed
stock of 24 Sep) are skipped and reported - the sim would show them as
pure shortage otherwise.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
from collections import defaultdict
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent
ITEMS = ROOT / "data" / "items.csv"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "xlsx",
        nargs="?",
        default="/home/ilham/Downloads/Warehouse Management System_16 Oktober 2026_.xlsx",
    )
    ap.add_argument("--out", default=str(ROOT / "data" / "schedule.json"))
    ap.add_argument("--sheet", default="Schedule of the day")
    a = ap.parse_args()

    upp = {}
    with open(ITEMS, newline="") as f:
        for row in csv.DictReader(f):
            upp[row["sku"]] = int(row["upp"] or 4)

    wb = openpyxl.load_workbook(a.xlsx, data_only=True, read_only=True)
    if a.sheet not in wb.sheetnames:
        raise SystemExit(f"sheet {a.sheet!r} not in {wb.sheetnames}")
    ws = wb[a.sheet]

    def iso(x):
        if x is None:
            return ""
        if isinstance(x, (dt.datetime, dt.date)):
            return x.strftime("%Y-%m-%d")
        return str(x).strip()

    def clock(x):
        if isinstance(x, (dt.datetime, dt.time)):
            t = x if isinstance(x, dt.time) else x.time()
            return t.strftime("%H:%M")
        if isinstance(x, dt.datetime):
            return x.strftime("%H:%M")
        if isinstance(x, str) and ":" in x:
            parts = x.strip().split(":")
            if parts[0].isdigit():
                return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
        return ""

    groups: dict[str, list] = defaultdict(list)
    skipped_rows = 0
    for r in ws.iter_rows(min_row=2, values_only=True):
        if r is None or len(r) < 20:
            continue
        sid = str(r[3]).strip() if r[3] is not None else ""
        mat = str(r[6]).strip() if r[6] is not None else ""
        if not sid or not mat:  # blank / total row
            continue
        try:
            qty = int(float(str(r[8]).replace(",", "")))
        except ValueError:
            continue
        line = dict(
            order=str(r[0] or ""),
            po=str(r[12] or ""),
            due=iso(r[13]),
            transport=str(r[14] or ""),
            truck_type=str(r[15] or ""),
            slot=clock(r[16]),
            no=int(r[17]) if str(r[17] or "").strip().isdigit() else 999,
            destination=str(r[4] or ""),
            city=str(r[5] or ""),
            mat=mat,
            qty=qty,
            unit=str(r[9] or ""),
        )
        groups[sid].append(line)

    shipments, skipped = [], []
    for sid, lines in groups.items():
        merged: dict[str, int] = defaultdict(int)
        for ln in lines:
            if ln["mat"] in upp:
                merged[ln["mat"]] += ln["qty"]
            else:
                skipped.append((sid, ln["mat"], ln["qty"]))
        if not merged:
            continue
        l0 = sorted(lines, key=lambda x: x["no"])[0]
        pallets = sum(math.ceil(q / upp[m]) for m, q in merged.items())
        shipments.append(
            dict(
                sid=sid,
                order=l0["order"],
                po=l0["po"],
                customer=l0["destination"],
                city=l0["city"],
                truck_type=l0["truck_type"],
                slot=l0["slot"],
                no=l0["no"],
                due=l0["due"],
                lines=[[m, q] for m, q in merged.items()],
                cartons=sum(merged.values()),
                est_pallets=pallets,
            )
        )

    # plan order: slot time, then the sheet's NO column
    shipments.sort(key=lambda s: (s["slot"] == "", s["slot"], s["no"]))

    data = dict(
        source=Path(a.xlsx).name,
        sheet=a.sheet,
        generated=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        n_shipments=len(shipments),
        cartons=sum(s["cartons"] for s in shipments),
        est_pallets=sum(s["est_pallets"] for s in shipments),
        skipped=[list(x) for x in skipped],
        shipments=shipments,
    )
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(data, indent=1))

    print(f"{a.sheet} ({Path(a.xlsx).name}):")
    for s in shipments:
        print(
            f"  slot {s['slot'] or '--'}  {s['sid']}  {s['truck_type']:5}  "
            f"{s['customer'][:26]:26} {s['city'][:12]:12} "
            f"{len(s['lines'])} SKU {s['cartons']:5} CAR ~{s['est_pallets']:2} pal  due {s['due']}"
        )
    if skipped:
        print(f"skipped (no seed stock): {skipped}")
    print(
        f"-> {a.out}: {len(shipments)} shipments, {data['cartons']} cartons, ~{data['est_pallets']} pallets"
    )


if __name__ == "__main__":
    main()
