#!/usr/bin/env python3
"""
CKB WSM SUB 2 (Surabaya) - warehouse discrete-event simulation in SimPy.

The rack layout, bin coordinates, pick path, phantom racks, pickfaces and the
opening stock all come from the WMS repository (github.com/mxxham/WMS):
    - supabase/migrations/0006_back_to_back_racks.sql   (coordinates)
    - lib/allocator/config.ts, pickpath.ts               (blocked bins, serpentine route)
    - supabase/seed.sql                                  (bins, items, stock of 24 Sep 2026)
Those were extracted once into data/bins.csv, data/items.csv, data/stock.csv.

Flows
    Inbound truck : gate queue -> dock door -> unload (pemuat) -> receiving area
                    -> checker (check + scan + assign a bin)
                    -> forklift (low levels) / stacker (top levels) put-away.
    Outbound truck: day-start allocation (FEFO, same rules as docs/ALLOCATOR.md)
                    -> forklift/stacker pallet picks + picker hand picks into a
                    staging lane -> checkers audit every line -> wait for the
                    (late) truck -> pemuat load -> truck leaves.
    Replenishment : forklift/stacker move leftovers of opened pallets and refill
                    empty pickfaces (bin-to-bin).

    Outbound orders: with --schedule data/schedule.json (from extract_schedule.py)
    the outbound trucks are the REAL shipments of the "Schedule of the day" sheet
    (arrival slots, customer, truck type); lateness is still simulated.

    Truck plan: --trucks-out N runs the N earliest scheduled shipments (extra
    trucks get synthetic orders); --trucks-in N sets the inbound trucks, spread
    through the morning.  Leaving both unset keeps --trucks as the day's total
    (every scheduled shipment + the remainder inbound).

Staff (per the request): pickers, checkers, 2 forklifts, 2 stackers (reach
trucks for the top of the rack), pemuat.  No operators/controllers/restockers.

Run:  python wms_sim.py --out out
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import statistics
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path

import simpy

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

# --------------------------------------------------------------------------- layout
# From the `layout` setting in 0001_schema.sql / 0006_back_to_back_racks.sql (placeholders in the
# repo too - change them here once you have measured values).
LAYOUT = dict(
    bay_width_m=2.7,
    rack_depth_m=1.2,
    level_height_m=1.6,
    aisle_width_m=3.2,
    positions_per_bay=2,
    bays_per_side=20,
    aisle_order=["CA", "CB", "CC", "CD", "CE", "CF", "CG"],
)
PITCH = (
    2 * LAYOUT["rack_depth_m"] + LAYOUT["aisle_width_m"]
)  # block-to-block distance 5.6 m
# lib/allocator/config.ts: pillars on the floor, never used
BLOCKED = {
    "CE33A01",
    "CE33A02",
    "CE33B01",
    "CE33B02",
    "CE33C01",
    "CE33C02",
    "CE33D01",
    "CE33D02",
    "CE33E01",
    "CE33E02",
    "CE32A02",
    "CE32B02",
    "CE32C02",
    "CE32D02",
    "CE32E02",
    "CE34A01",
    "CE34B01",
    "CE34C01",
    "CE34D01",
    "CE34E01",
}
RACK_RE = re.compile(
    r"^(C[A-F])(\d{2})([A-E])(\d{2})$"
)  # aisle CG excluded (does not exist)
LEVELS = "ABCDE"

# Floor positions are NOT in the repo for docks/receiving; the staging lanes are (0006: floor row).
# Site orientation: the dock wall sits on the SHORT side (bay-01 end) — from a gate you look
# down the aisles to bay 20. All operational floor is west of WEST_X, gates spread across z.
WEST_X, EAST_X = -2.5, 56.5  # cross aisles; WEST_X is the dock-side front corridor
FRONT = -1  # "lane" id of the open floor west of the racks (x < -2.5)
STAGING_X = -8.0  # staging lanes line up along the west floor
DOOR_X = -14.5  # dock wall line
DOORS = [(DOOR_X, -1.5 + 5.0 * d) for d in range(7)]  # 7 gates, 2-3 normally active
TRUCK_X = -19.0  # centre line of a docked truck (nose at the dock wall)
RECV_X, RECV_Z0, RECV_DZ = -9.5, 15.0, 0.9  # receiving area (assumption)
DESK = (-5.5, -2.5, FRONT)  # inventory controller's desk
HOME = (-5.5, -0.5, FRONT)  # where idle staff wait


def lane_z(lane: int) -> float:
    """Centre line of walking lane `lane` (lane i is left of block i, lane i+1 right of it)."""
    return -PITCH / 2 + PITCH * lane


def route(a, b):
    """Polyline between two (x, z, lane) points following the aisles. Returns (points, length)."""
    ax, az, al = a
    bx, bz, bl = b
    options = []
    if al == bl:
        options.append(
            [(ax, az), (bx, az), (bx, bz)] if al == FRONT else [(ax, az), (bx, bz)]
        )
    elif {al, bl} == {FRONT, 0}:
        # lane 0 is open towards the front floor along its whole length
        options.append(
            [(ax, az), (bx, az), (bx, bz)]
            if al == FRONT
            else [(ax, az), (ax, bz), (bx, bz)]
        )
    for X in (WEST_X, EAST_X):
        options.append([(ax, az), (X, az), (X, bz), (bx, bz)])
    best = min(options, key=lambda p: _plen(p))
    return best, _plen(best)


def _plen(pts):
    return sum(
        abs(pts[i + 1][0] - pts[i][0]) + abs(pts[i + 1][1] - pts[i][1])
        for i in range(len(pts) - 1)
    )


# --------------------------------------------------------------------------- data model
class Item:
    def __init__(self, row):
        self.sku = row["sku"]
        self.desc = row["description"]
        self.upp = max(1, int(float(row["upp"] or 1)))
        self.vol = float(row["volume_l"] or 1)
        self.abc = row["abc_class"] or "C"


class Bin:
    __slots__ = (
        "code",
        "idx",
        "aisle",
        "rack",
        "level",
        "lv",
        "pos",
        "bay",
        "face",
        "x",
        "y",
        "z",
        "lane",
        "pallet",
        "claim",
        "key",
    )

    def __init__(self, code, idx):
        m = RACK_RE.match(code)
        self.code, self.idx = code, idx
        self.aisle, self.rack, self.level, self.pos = (
            m.group(1),
            int(m.group(2)),
            m.group(3),
            int(m.group(4)),
        )
        self.lv = LEVELS.index(self.level)
        L = LAYOUT
        n = L["bays_per_side"]
        a_idx = L["aisle_order"].index(self.aisle)
        self.face = 1 if self.rack > n else 0
        self.bay = (self.rack - 1) % n + 1
        # exact formulas of recompute_bin_positions() in 0006_back_to_back_racks.sql
        self.x = (self.rack - 1) % n * L["bay_width_m"] + (self.pos - 0.5) * L[
            "bay_width_m"
        ] / L["positions_per_bay"]
        self.y = self.lv * LAYOUT["level_height_m"]
        self.z = a_idx * PITCH + (
            L["rack_depth_m"] / 2 if self.face else -L["rack_depth_m"] / 2
        )
        self.lane = a_idx + self.face
        self.pallet = None
        self.claim = None  # pallet that is on its way into this bin
        # lib/allocator/pickpath.ts pickSequenceKey (serpentine)
        along = self.bay
        along_rank = 99 - along if self.lane % 2 == 1 else along
        self.key = (
            self.lane * 10_000_000
            + along_rank * 100_000
            + self.lv * 1_000
            + self.face * 100
            + self.pos
        )

    def access(self):
        return (self.x, lane_z(self.lane), self.lane)


class Pallet:
    _n = 0

    def __init__(self, env, sku, batch, expiry, qty):
        Pallet._n += 1
        self.id = Pallet._n
        self.sku, self.batch, self.expiry, self.qty = sku, batch, expiry, qty
        self.reserved = 0
        self.bin = None
        self.loc = None  # floor position when not in a bin
        self.lock = simpy.Resource(env, 1)
        self.relocating = False

    def access(self):
        return self.bin.access() if self.bin else self.loc

    def key(self):
        return self.bin.key if self.bin else 0


@dataclass
class Task:
    kind: str  # PALLET (forklift) | CASE (hand pick)
    pallet: Pallet
    take: int
    line: int = 0


class Agent:
    def __init__(self, name, role, home):
        self.name, self.role = name, role
        self.x, self.z, self.lane = home
        self.home = home
        self.segs = []  # [t0, t1, [(x,z),...], label, carry]
        self.busy = 0.0
        self._t0 = 0.0

    def pos(self):
        return (self.x, self.z, self.lane)


class Pool:
    def __init__(self, env, role, n, home):
        self.role = role
        self.store = simpy.Store(env)
        self.agents = [
            Agent(f"{role[:3].upper()}{i + 1}", role, home) for i in range(n)
        ]
        for a in self.agents:
            self.store.put(a)
        self.waits = []

    @property
    def queue(self):
        return len(self.store.get_queue)


@dataclass
class Shipment:
    sid: str
    truck: int
    slot_t: float
    lines: list
    tasks: list = field(default_factory=list)
    short: list = field(default_factory=list)
    lane: int = -1
    alloc_done: object = None
    ready: object = None
    docked: object = None
    loaded: object = None
    ready_t: float = None
    loaded_t: float = None
    staged: int = 0  # pallets standing in the staging lane
    cases_cartons: int = 0
    audit_fail: int = 0
    dest: str = ""  # real order info (when --schedule is used)
    city: str = ""
    ttype: str = ""
    po: str = ""
    due: str = ""


@dataclass
class Truck:
    tid: int
    kind: str  # IN | OUT
    sched: float
    arrival: float
    pallets: list = field(default_factory=list)  # inbound pallets
    ship: Shipment = None
    agent: Agent = None
    t_gate: float = None
    t_dock: float = None
    t_done: float = None
    t_leave: float = None
    door: int = -1
    states: list = field(default_factory=list)


# --------------------------------------------------------------------------- configuration
@dataclass
class Config:
    seed: int = 7
    start_hhmm: str = "07:00"
    # staff and equipment (from the request)
    n_pickers: int = 2
    n_checkers: int = 2
    n_stackers: int = 2  # reach trucks, put away / pick from the TOP levels
    n_forklifts: int = 2  # low levels and floor moves
    n_loaders: int = 4  # pemuat
    stacker_min_lv: int = 2  # level index (0=A .. 4=E); >= this -> stacker
    dock_doors: int = 2  # 7 gates built; 2 normally used (sometimes 3)
    staging_lanes: int = 8  # STG_01..STG_08 from the seed
    recv_spots: int = 16
    # trucks
    n_trucks: int = 12  # legacy total; inbound = n_trucks - scheduled outbound
    n_trucks_in: int = -1  # inbound trucks today; -1 = derive from n_trucks
    n_trucks_out: int = -1  # outbound trucks today; -1 = every scheduled shipment
    first_slot_min: int = 30
    slot_gap_min: int = 35
    p_on_time: float = 0.35
    sd_on_time_min: float = 6
    p_early: float = 0.10
    late_mean_min: float = 75
    late_cap_min: float = 240
    arrival_order: tuple = ()  # e.g. (2, 4, 6, 1, 7, 9): these trucks arrive first, in that order
    schedule_file: str = (
        ""  # data/schedule.json from extract_schedule.py: real outbound orders
    )
    inbound_pallets: tuple = (10, 20)
    outbound_lines: tuple = (3, 7)
    load_factor: float = 1.0
    pick_lead_min: float = (
        90  # picking of a shipment is released this long before its slot
    )
    # speeds (m/s)
    fork_empty: float = 2.2
    fork_loaded: float = 1.6
    walk: float = 1.3
    walk_cart: float = 1.0
    jack: float = 1.0
    truck_speed: float = 3.0
    # work content (seconds)
    t_unload_pallet: float = 25
    t_load_pallet: float = 25
    t_scan: float = 25
    t_check_pallet: float = 70
    t_doc_check: float = 180
    t_assign: float = 30
    t_alloc_base: float = 90
    t_alloc_line: float = 40
    t_audit_line: float = 60
    p_audit_fail: float = 0.04
    t_audit_fix: float = 240
    t_fork_pos: float = 14  # positioning at a rack face
    fork_mast: float = 0.35  # m/s
    t_pick_base: float = 12
    t_drop: float = 10


# --------------------------------------------------------------------------- simulation
class Sim:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.env = simpy.Environment()
        self.rng = random.Random(cfg.seed)
        self.t0 = self._clock(cfg.start_hhmm)
        self.asof = date(2026, 9, 25)
        self.load_world()
        self.build_resources()
        self.log = dict(bin_ev=[], recv_ev=[], lane_ev=[], series=[], notes=[])
        self.stats = dict(
            recv=0,
            stored=0,
            shipped=0,
            picked_cartons=0,
            fl_picks=0,
            hp_picks=0,
            replen=0,
            reloc=0,
            audits=0,
            audit_fail=0,
            shortage_cartons=0,
        )
        self.bg = []
        self.finished = False

    # ---- world ----
    @staticmethod
    def _clock(hhmm):
        h, m = hhmm.split(":")
        return int(h) * 3600 + int(m) * 60

    def hhmm(self, t):
        s = int(self.t0 + t)
        return f"{s // 3600 % 24:02d}:{s // 60 % 60:02d}"

    def load_world(self):
        self.items = {
            r["sku"]: Item(r) for r in csv.DictReader(open(DATA / "items.csv"))
        }
        rows = [r for r in csv.DictReader(open(DATA / "bins.csv"))]
        codes = sorted(
            r["bin_code"]
            for r in rows
            if RACK_RE.match(r["bin_code"]) and r["bin_code"] not in BLOCKED
        )
        self.bins = {}
        self.bin_list = []
        for c in codes:
            b = Bin(c, len(self.bin_list))
            self.bins[c] = b
            self.bin_list.append(b)
        self.sku_pallets = {s: set() for s in self.items}
        self.initial_cartons = 0
        self.inbound_cartons = 0  # cartons received today, fixed at creation
        self.excluded_cartons = 0
        for r in csv.DictReader(open(DATA / "stock.csv")):
            b = self.bins.get(r["bin_code"])
            qty = int(float(r["qty"]))
            if b is None:  # QUARANTINE etc.
                self.excluded_cartons += qty
                continue
            exp = date.fromisoformat(r["expiry"]) if r["expiry"] else date(2030, 1, 1)
            if (
                exp.year < 2000
            ):  # CE08A01 expired "1930": a known data issue (docs/DATA_ISSUES.md)
                exp = date(2030, 1, 1)
            p = Pallet(self.env, r["sku"], r["batch"], exp, qty)
            p.bin, b.pallet = b, p
            self.sku_pallets[p.sku].add(p)
            self.initial_cartons += qty
        # pickface per SKU = earliest level-A bin on the pick path that holds it (docs/ALLOCATOR.md)
        self.pickface = {}
        for sku, ps in self.sku_pallets.items():
            a = [p.bin for p in ps if p.bin and p.bin.level == "A"]
            if a:
                self.pickface[sku] = min(a, key=lambda b: b.key)
        self.initial_state = [self.frac(b) for b in self.bin_list]
        self.initial_pickfaces = {s: b.code for s, b in self.pickface.items()}

    def frac(self, b):
        p = b.pallet
        return round(min(1.0, p.qty / self.items[p.sku].upp), 2) if p else 0.0

    def build_resources(self):
        c, env = self.cfg, self.env
        self.pickers = Pool(env, "picker", c.n_pickers, HOME)
        self.checkers = Pool(env, "checker", c.n_checkers, HOME)
        self.stackers = Pool(env, "stacker", c.n_stackers, (-7.0, -12.0, FRONT))
        self.forklifts = Pool(env, "forklift", c.n_forklifts, (-4.0, -11.0, FRONT))
        self.loaders = Pool(env, "loader", c.n_loaders, HOME)
        self.pools = [
            self.pickers,
            self.checkers,
            self.stackers,
            self.forklifts,
            self.loaders,
        ]
        self.doors = simpy.Store(env)
        for i in range(c.dock_doors):
            self.doors.put(i)
        self.lanes = simpy.Store(env)
        for i in range(c.staging_lanes):
            self.lanes.put(i)
        self.recv = simpy.Store(env)
        for i in range(c.recv_spots):
            self.recv.put(i)
        self.wait_spots = list(range(40))
        self.agents = [a for p in self.pools for a in p.agents]

    # ---- helpers ----
    def lane_pt(
        self, i
    ):  # STG_01.. : staged along the west floor, z = (k)*1.4, k = 2..
        return (STAGING_X, (i + 2) * 1.4, FRONT)

    def recv_pt(self, i):
        return (RECV_X, RECV_Z0 + RECV_DZ * i, FRONT)

    def door_pt(self, d):
        return (DOORS[d][0], DOORS[d][1], FRONT)

    def lift_time(self, lv):
        return (
            self.cfg.t_fork_pos
            + 2 * (lv * LAYOUT["level_height_m"]) / self.cfg.fork_mast / 2
            + 4
        )

    def bin_ev(self, b, flag):
        self.log["bin_ev"].append(
            [
                round(self.env.now, 1),
                b.idx,
                self.frac(b),
                1 if b.pallet and b.pallet.reserved > 0 else 0,
                flag,
            ]
        )

    def note(self, text):
        self.log["notes"].append([round(self.env.now, 1), text])

    def grab(self, pool):
        t = self.env.now
        a = yield pool.store.get()
        pool.waits.append(self.env.now - t)
        a._t0 = self.env.now
        return a

    def release(self, pool, a):
        a.busy += self.env.now - a._t0
        pool.store.put(a)

    def travel(self, agents, dest, speed, label, carry=0):
        a0 = agents[0]
        pts, dist = route(a0.pos(), dest)
        dur = dist / speed
        if dur > 0:
            t = self.env.now
            seg = [
                round(t, 1),
                round(t + dur, 1),
                [(round(x, 2), round(z, 2)) for x, z in pts],
                label,
                carry,
            ]
            for a in agents:
                a.segs.append(seg)
            yield self.env.timeout(dur)
        for a in agents:
            a.x, a.z, a.lane = dest

    def act(self, agents, dur, label, carry=0):
        if dur <= 0:
            return
        t = self.env.now
        a0 = agents[0]
        seg = [
            round(t, 1),
            round(t + dur, 1),
            [(round(a0.x, 2), round(a0.z, 2))],
            label,
            carry,
        ]
        for a in agents:
            a.segs.append(seg)
        yield self.env.timeout(dur)

    # ---- trucks and demand ----
    def load_schedule(self):
        """Real outbound shipments from the 'Schedule of the day' sheet
        (extract_schedule.py -> data/schedule.json).  Lines for materials that
        are not in the seed stock are skipped and reported as a pure shortage."""
        if not self.cfg.schedule_file:
            return []
        try:
            data = json.loads(Path(self.cfg.schedule_file).read_text())
        except Exception as e:
            print(
                f"WARN: schedule {self.cfg.schedule_file} unreadable ({e}) - synthetic trucks"
            )
            return []
        valid, unknown = [], {}
        for s in data.get("shipments", []):
            lines = []
            for m, q in s["lines"]:
                m = str(m).strip()
                if m in self.items:
                    lines.append((m, int(q)))
                else:
                    unknown[m] = unknown.get(m, 0) + int(q)
            if not lines:
                continue
            valid.append(s | {"lines": lines})
        if unknown:
            self.note(
                "schedule: skipped SKUs without opening stock: "
                + ", ".join(f"{m}({q})" for m, q in sorted(unknown.items()))
            )
        n_skip = len(data.get("shipments", [])) - len(valid)
        self.n_real_ships = len(valid)
        self.note(
            f"Day plan: {len(valid)} real shipments from the 'Schedule of the day' sheet"
            f" ({Path(data.get('source', '')).name})"
            f"{f', {n_skip} dropped (no stock lines)' if n_skip else ''}"
        )
        return valid

    def _slot_t(self, slot, fallback_idx):
        """Wall-clock HH:MM -> simulated seconds since start (07:00)."""
        c = self.cfg
        if slot and ":" in slot:
            hh, mm = slot.split(":")[:2]
            mins = (
                int(hh) * 60
                + int(mm)
                - int(c.start_hhmm.split(":")[0]) * 60
                - int(c.start_hhmm.split(":")[1])
            )
            return max(0.0, mins * 60.0)
        return (c.first_slot_min + fallback_idx * c.slot_gap_min) * 60

    def _in_slots(self, k):
        """Synthetic inbound truck slots: spread through the early shift.

        One truck keeps the old 07:30 slot; more trucks widen the window
        (about 30 min apart) so they do not all queue at the gate at once."""
        if k <= 0:
            return []
        start, span = 5.0, max(50.0, 30.0 * k)
        step = span / k
        return [(start + (j + 0.5) * step) * 60 for j in range(k)]

    def _truck_mix(self, n_real):
        """Outbound / inbound truck counts for the day.

        Explicit Config counts (`n_trucks_out`, `n_trucks_in`) win.  Otherwise
        the legacy rule holds: `n_trucks` is the day's total, every scheduled
        shipment keeps its truck, and the remainder is inbound."""
        c = self.cfg
        n_out, n_in = c.n_trucks_out, c.n_trucks_in
        if n_out < 0 and n_in < 0:
            n_in = max(0, c.n_trucks - n_real) if n_real else c.n_trucks // 2
            n_out = max(n_real, c.n_trucks - n_in)
        elif n_out < 0:
            n_out = max(n_real, c.n_trucks - n_in)
        elif n_in < 0:
            n_in = max(0, c.n_trucks - n_out)
        return max(0, int(n_out)), max(0, int(n_in))

    def make_trucks(self):
        c, rng, env = self.cfg, self.rng, self.env
        ships_real = self.load_schedule()
        n_sched = len(ships_real)
        n_out, n_in = self._truck_mix(n_sched)
        # a trimmed day keeps the earliest slots
        ships_real.sort(key=lambda s: self._slot_t(s.get("slot", ""), 0))
        ships_real = ships_real[:n_out]
        n_real = len(ships_real)
        n_synth_out = n_out - n_real
        n = n_out + n_in
        if not n_real:  # no schedule at all: interleave inbound and outbound
            kinds = ["IN"] * n_in + ["OUT"] * n_out
            rng.shuffle(kinds)
        else:
            kinds = ["OUT"] * n_out + ["IN"] * n_in
        in_slots = self._in_slots(n_in)
        last_sched = max(
            (self._slot_t(s.get("slot", ""), k) for k, s in enumerate(ships_real)),
            default=0.0,
        )
        self.n_real_ships = n_real
        self.truck_plan = dict(
            outbound=n_out,
            inbound=n_in,
            scheduled_in_file=n_sched,
            scheduled=n_real,
            synthetic_out=n_synth_out,
            synthetic_in=n_in,
        )
        if n_sched and n_real < n_sched:
            self.note(
                f"Truck plan: {n_real} of {n_sched} scheduled shipments run today"
            )
        self.note(
            f"Truck plan: {n_out} outbound "
            f"({n_real} scheduled + {n_synth_out} synthetic)"
            f" and {n_in} inbound = {n} trucks"
        )
        trucks = []
        k_in = 0
        for i in range(1, n + 1):
            if i <= n_real:
                s = ships_real[i - 1]
                sched = self._slot_t(s.get("slot", ""), i - 1)
                truck = Truck(i, "OUT", sched, None)
                sh = Shipment(
                    str(s["sid"]), i, sched, [(str(m), int(q)) for m, q in s["lines"]]
                )
                sh.alloc_done, sh.ready = env.event(), env.event()
                sh.docked, sh.loaded = env.event(), env.event()
                sh.dest = s.get("customer", "")
                sh.city = s.get("city", "")
                sh.ttype = s.get("truck_type", "")
                sh.po = s.get("po", "")
                sh.due = s.get("due", "")
                truck.ship = sh
            else:
                if not n_real:  # synthetic plan: slots spread by truck number
                    sched = (c.first_slot_min + (i - 1) * c.slot_gap_min) * 60
                elif kinds[i - 1] == "OUT":  # extra outbound after the schedule
                    sched = last_sched + (i - n_real) * c.slot_gap_min * 60
                else:  # inbound
                    sched = in_slots[k_in]
                    k_in += 1
                truck = Truck(i, kinds[i - 1], sched, None)
            u = rng.random()
            if u < c.p_on_time:
                delay = rng.gauss(0, c.sd_on_time_min * 60)
            elif u < c.p_on_time + c.p_early:
                delay = -rng.uniform(5, 25) * 60
            else:
                delay = min(
                    c.late_cap_min * 60,
                    rng.expovariate(1 / (c.late_mean_min * 60)) + 10 * 60,
                )
            truck.arrival = max(60.0, truck.sched + delay)
            trucks.append(truck)
        if c.arrival_order:
            times = sorted(t.arrival for t in trucks)
            forced = list(c.arrival_order)
            rest = [t.tid for t in trucks if t.tid not in forced]
            for tid, tm in zip(forced + rest, times):
                trucks[tid - 1].arrival = tm
        skus_in_stock = [s for s, ps in self.sku_pallets.items() if ps]
        wts = [{"A": 3, "B": 2, "C": 1}[self.items[s].abc] for s in skus_in_stock]
        for t in trucks:
            if t.kind == "IN":
                n_p = max(1, round(rng.randint(*c.inbound_pallets) * c.load_factor))
                for _ in range(n_p):
                    sku = rng.choices(skus_in_stock, wts)[0]
                    it = self.items[sku]
                    qty = (
                        it.upp
                        if rng.random() < 0.85
                        else max(1, int(it.upp * rng.uniform(0.3, 0.9)))
                    )
                    exp = date(self.asof.year + 4, self.asof.month, 1)
                    batch = f"IN{t.tid:02d}{rng.randint(100, 999)}"
                    t.pallets.append(Pallet(self.env, sku, batch, exp, qty))
                    self.inbound_cartons += qty
            elif t.ship is not None:  # real shipment from the schedule
                continue
            else:
                lines = []
                for _ in range(rng.randint(*c.outbound_lines)):
                    sku = rng.choices(skus_in_stock, wts)[0]
                    it = self.items[sku]
                    full = rng.choice([0, 1, 1, 2, 3])
                    loose = (
                        rng.randint(1, it.upp - 1)
                        if it.upp > 1 and rng.random() < 0.6
                        else 0
                    )
                    q = round((full * it.upp + loose) * c.load_factor)
                    lines.append((sku, q if q > 0 else it.upp))
                t.ship = Shipment(f"SH{t.tid:02d}", t.tid, t.sched, lines)
                env = self.env
                t.ship.alloc_done, t.ship.ready = env.event(), env.event()
                t.ship.docked, t.ship.loaded = env.event(), env.event()
        return trucks

    # ---- FEFO allocation (docs/ALLOCATOR.md, simplified) ----
    def allocate_line(self, sku, need):
        upp = self.items[sku].upp
        tasks, rem = [], need
        pf_bin = self.pickface.get(sku)
        while rem > 0:
            cands = [p for p in self.sku_pallets[sku] if p.qty - p.reserved > 0]
            if not cands:
                break
            e0 = min(p.expiry for p in cands)  # FEFO is absolute
            grp = [p for p in cands if p.expiry == e0]
            pf = pf_bin.pallet if pf_bin else None
            avail = lambda p: p.qty - p.reserved
            whole = [p for p in grp if p.reserved == 0 and upp <= p.qty <= rem]
            if whole and rem >= upp:  # 1. sealed full pallets, reserve first
                whole.sort(key=lambda p: (p is pf, p.key()))
                p = whole[0]
                take = p.qty
                kind = "PALLET"
            else:  # loose cartons
                kind = "CASE"
                if pf in grp and avail(pf) >= rem:
                    p, take = pf, rem
                else:
                    cover = [
                        p
                        for p in grp
                        if p is not pf and avail(p) >= rem and p.qty < upp
                    ]
                    if cover:
                        p, take = min(cover, key=avail), rem
                    elif pf in grp and avail(pf) > 0:
                        p, take = pf, avail(pf)
                    else:
                        opened = [
                            p
                            for p in grp
                            if p is not pf and avail(p) > 0 and p.qty < upp
                        ]
                        pool = (
                            opened
                            or [p for p in grp if p is not pf and avail(p) > 0]
                            or [p for p in grp if avail(p) > 0]
                        )
                        p = min(pool, key=lambda x: (abs(avail(x) - rem), x.key()))
                        take = min(avail(p), rem)
            p.reserved += take
            tasks.append(Task(kind, p, take))
            rem -= take
        return tasks, rem

    def allocate_shipments(self, ships):
        """Day-start allocation (system step; the checker hands the picklist out)."""
        for sh in sorted(ships, key=lambda s: (s.slot_t, s.sid)):
            yield self.env.timeout(
                self.cfg.t_alloc_base + self.cfg.t_alloc_line * len(sh.lines)
            )
            for li, (sku, q) in enumerate(sh.lines):
                tasks, short = self.allocate_line(sku, q)
                for t in tasks:
                    t.line = li
                sh.tasks += tasks
                if short:
                    sh.short.append((sku, short))
                    self.stats["shortage_cartons"] += short
            for t in sh.tasks:
                if t.pallet.bin:
                    self.bin_ev(t.pallet.bin, 4)
            sh.alloc_done.succeed()
            self.note(
                f"{self.hhmm(self.env.now)} {sh.sid}: allocated {len(sh.tasks)} tasks"
                f"{' (shortage ' + str(sum(q for _, q in sh.short)) + ')' if sh.short else ''}"
            )

    # ---- movement of pallets by forklift / stacker ----
    def veh(self, lv):
        """Vehicle for a rack level: reach truck (stacker) at the top, forklift below."""
        return self.stackers if lv >= self.cfg.stacker_min_lv else self.forklifts

    def pick_up(self, p, fk):
        """The vehicle takes a pallet out of its bin (or off the floor)."""
        b = p.bin
        if b is not None:
            yield from self.act([fk], self.lift_time(b.lv), "lift")
            b.pallet = None
            p.loc = b.access()
            p.bin = None
            self.bin_ev(b, 3)
        else:
            yield from self.act([fk], self.cfg.t_fork_pos, "lift")

    def put_down(self, p, b, fk):
        yield from self.act([fk], self.lift_time(b.lv), "place")
        b.pallet = p
        b.claim = None
        p.bin, p.loc = b, None
        self.bin_ev(b, 3)

    def carry_job(self, p, target, label):
        """Forklift or stacker moves pallet p into `target` bin.

        The vehicle itself drives it: the stacker (reach truck) handles the top
        levels, the forklift the lower ones and the floor."""
        pool = self.veh(target.lv)
        fk = yield from self.grab(pool)
        yield from self.travel([fk], p.access(), self.cfg.fork_empty, "to_pallet")
        with p.lock.request() as rq:
            yield rq
            yield from self.pick_up(p, fk)
            yield from self.travel(
                [fk], target.access(), self.cfg.fork_loaded, label, carry=1
            )
            yield from self.put_down(p, target, fk)
        self.release(pool, fk)

    def nearest_empty(self, from_pt, levels="ABCDE", pickfaces_ok=False):
        pf_set = {b.code for b in self.pickface.values()}
        best, best_d = None, 1e18
        for b in self.bin_list:
            if (
                b.pallet is None
                and b.claim is None
                and b.level in levels
                and (pickfaces_ok or b.code not in pf_set)
            ):
                d = _plen(route(from_pt, b.access())[0]) + b.lv * 1.5
                if d < best_d:
                    best, best_d = b, d
        return best

    # ---- background jobs ----
    def relocate_leftover(self, p):
        """Bin-to-bin: leftover of an opened pallet goes to the SKU's pickface (if empty) or the nearest empty level-A bin."""
        pf = self.pickface.get(p.sku)
        if pf and pf.pallet is None and pf.claim is None:
            tgt = pf
        else:
            tgt = self.nearest_empty(p.access(), "A") or self.nearest_empty(p.access())
        if tgt is None:
            p.relocating = False
            return
        tgt.claim = p
        yield from self.carry_job(p, tgt, "bin_to_bin")
        p.relocating = False
        self.stats["reloc"] += 1

    def maybe_replen(self, sku):
        pf = self.pickface.get(sku)
        if not pf or pf.pallet is not None or pf.claim is not None:
            return
        src = [
            p
            for p in self.sku_pallets[sku]
            if p.bin and p.reserved == 0 and p.bin is not pf and p.qty > 0
        ]
        if not src:
            return
        e0 = min(p.expiry for p in src)
        p = min(
            (p for p in src if p.expiry == e0),
            key=lambda x: _plen(route(pf.access(), x.access())[0]),
        )
        pf.claim = p
        p.reserved += p.qty  # keep allocation away while it travels
        self.spawn(self._replen(p, pf))

    def _replen(self, p, pf):
        yield from self.carry_job(p, pf, "replenish")
        p.reserved = 0
        self.stats["replen"] += 1
        self.bin_ev(pf, 3)

    def spawn(self, gen):
        pr = self.env.process(gen)
        self.bg.append(pr)
        return pr

    # ---- inbound ----
    def receive_pallet(self, truck, p, spot):
        c = self.cfg
        ck = yield from self.grab(self.checkers)
        yield from self.travel([ck], self.recv_pt(spot), c.walk, "walk")
        yield from self.act([ck], c.t_check_pallet + c.t_scan + c.t_assign, "check")
        pf = self.pickface.get(p.sku)
        if pf and pf.pallet is None and pf.claim is None:
            tgt = pf
        else:
            tgt = self.nearest_empty(self.recv_pt(spot), "BCDE") or self.nearest_empty(
                self.recv_pt(spot)
            )
        tgt.claim = p
        self.release(self.checkers, ck)
        p.loc = self.recv_pt(spot)
        yield from self.carry_job(p, tgt, "putaway")
        self.log["recv_ev"].append([round(self.env.now, 1), spot, 0])
        self.recv.put(spot)
        self.sku_pallets[p.sku].add(p)
        self.stats["stored"] += 1

    def unload_pallet(self, truck, p):
        c = self.cfg
        ld = yield from self.grab(self.loaders)
        yield from self.travel([ld], self.door_pt(truck.door), c.jack, "walk")
        yield from self.act([ld], c.t_unload_pallet, "unload")
        spot = yield self.recv.get()
        yield from self.travel([ld], self.recv_pt(spot), c.jack, "move", carry=1)
        p.loc = self.recv_pt(spot)
        self.log["recv_ev"].append([round(self.env.now, 1), spot, 1])
        self.release(self.loaders, ld)
        self.stats["recv"] += 1
        self.spawn(self.receive_pallet(truck, p, spot))

    # ---- outbound ----
    def fl_picklist(self, sh, tasks):
        c = self.cfg
        for t in sorted(tasks, key=lambda t: t.pallet.key()):
            p = t.pallet
            pool = self.veh(p.bin.lv if p.bin else 0)
            fk = yield from self.grab(pool)
            yield from self.travel([fk], p.access(), c.fork_empty, "to_pallet")
            with p.lock.request() as rq:
                yield rq
                was_pf = p.bin is not None and self.pickface.get(p.sku) is p.bin
                yield from self.pick_up(p, fk)
                yield from self.travel(
                    [fk],
                    self.lane_pt(sh.lane),
                    c.fork_loaded,
                    "to_staging",
                    carry=1,
                )
                yield from self.act([fk], c.t_drop, "drop")
                p.loc = self.lane_pt(sh.lane)
                self.sku_pallets[p.sku].discard(p)
                p.qty -= t.take
                p.reserved -= t.take
            self.release(pool, fk)
            sh.staged += 1
            self.stats["fl_picks"] += 1
            self.stats["picked_cartons"] += t.take
            self.log["lane_ev"].append(
                [round(self.env.now, 1), sh.lane, sh.staged, sh.truck]
            )
            if was_pf:
                self.maybe_replen(p.sku)

    def lower_pallet(self, p):
        """A pallet in a rack level B-E is brought down to the floor so a picker can open it."""
        pool = self.veh(p.bin.lv if p.bin else 0)
        fk = yield from self.grab(pool)
        yield from self.travel([fk], p.access(), self.cfg.fork_empty, "to_pallet")
        with p.lock.request() as rq:
            yield rq
            if p.bin is not None:
                yield from self.pick_up(p, fk)
                yield from self.act([fk], 8, "place")
        self.release(pool, fk)

    def hp_picklist(self, sh, tasks):
        c = self.cfg
        pk = yield from self.grab(self.pickers)
        carry = 0
        for t in sorted(tasks, key=lambda t: t.pallet.key()):
            p = t.pallet
            if p.bin is not None and p.bin.level != "A":
                yield from self.travel(
                    [pk], p.access(), c.walk_cart if carry else c.walk, "walk", carry
                )
                yield from self.lower_pallet(p)
            yield from self.travel(
                [pk], p.access(), c.walk_cart if carry else c.walk, "walk", carry
            )
            it = self.items[p.sku]
            with p.lock.request() as rq:
                yield rq
                yield from self.act(
                    [pk], c.t_pick_base + t.take * (4 + 0.05 * it.vol), "pick", carry
                )
                was_pf = p.bin is not None and self.pickface.get(p.sku) is p.bin
                b = p.bin
                p.qty -= t.take
                p.reserved -= t.take
            carry = 1
            sh.cases_cartons += t.take
            self.stats["hp_picks"] += 1
            self.stats["picked_cartons"] += t.take
            if p.qty == 0:
                if b is not None:
                    b.pallet = None
                    self.bin_ev(b, 1)
                self.sku_pallets[p.sku].discard(p)
                if was_pf:
                    self.maybe_replen(p.sku)
            else:
                if b is not None:
                    self.bin_ev(b, 1)
                elif (
                    p.reserved == 0 and not p.relocating
                ):  # opened pallet on the floor -> bin-to-bin
                    p.relocating = True
                    self.spawn(self.relocate_leftover(p))
        yield from self.travel(
            [pk], self.lane_pt(sh.lane), c.walk_cart, "to_staging", 1
        )
        yield from self.act([pk], c.t_drop, "drop", 1)
        self.release(self.pickers, pk)

    def audit_line(self, sh, li):
        c = self.cfg
        ck = yield from self.grab(self.checkers)
        yield from self.travel([ck], self.lane_pt(sh.lane), c.walk, "walk")
        yield from self.act([ck], c.t_audit_line, "audit")
        self.stats["audits"] += 1
        if self.rng.random() < c.p_audit_fail:
            sh.audit_fail += 1
            self.stats["audit_fail"] += 1
            yield from self.act([ck], c.t_audit_fix, "fix")
        self.release(self.checkers, ck)

    def shipment_proc(self, sh):
        c, env = self.cfg, self.env
        yield sh.alloc_done
        release_t = sh.slot_t - c.pick_lead_min * 60
        if env.now < release_t:
            yield env.timeout(release_t - env.now)
        sh.lane = yield self.lanes.get()
        self.log["lane_ev"].append([round(env.now, 1), sh.lane, 0, sh.truck])
        fl = [t for t in sh.tasks if t.kind == "PALLET"]
        hp = [t for t in sh.tasks if t.kind == "CASE"]
        procs = []
        if fl:
            procs.append(env.process(self.fl_picklist(sh, fl)))
        if hp:
            procs.append(env.process(self.hp_picklist(sh, hp)))
        if procs:
            yield env.all_of(procs)
        lines = sorted({t.line for t in sh.tasks})
        aud = [env.process(self.audit_line(sh, li)) for li in lines]
        if aud:
            yield env.all_of(aud)
        sh.ready_t = env.now
        sh.ready.succeed()
        self.note(
            f"{self.hhmm(env.now)} {sh.sid}: picked and audited, waiting for truck {sh.truck}"
        )
        yield sh.docked
        n_mixed = (
            math.ceil(sum(t.take / self.items[t.pallet.sku].upp for t in hp))
            if hp
            else 0
        )
        n_load = sh.staged + n_mixed
        door = self.trucks[sh.truck - 1].door
        loads = [env.process(self.load_one(sh, door)) for _ in range(n_load)]
        if loads:
            yield env.all_of(loads)
        sh.loaded_t = env.now
        self.stats["shipped"] += n_load
        self.log["lane_ev"].append([round(env.now, 1), sh.lane, -1, sh.truck])
        self.lanes.put(sh.lane)
        sh.loaded.succeed()

    def load_one(self, sh, door):
        c = self.cfg
        ld = yield from self.grab(self.loaders)
        yield from self.travel([ld], self.lane_pt(sh.lane), c.jack, "walk")
        yield from self.act([ld], c.t_load_pallet, "load")
        yield from self.travel([ld], self.door_pt(door), c.jack, "move", 1)
        yield from self.act([ld], 12, "load", 1)
        self.release(self.loaders, ld)

    # ---- trucks ----
    def tlog(self, tr, state):
        tr.states.append([round(self.env.now, 1), state])

    def truck_proc(self, tr):
        c, env = self.cfg, self.env
        a = tr.agent
        yield env.timeout(max(0.0, tr.arrival - env.now))
        tr.t_gate = env.now
        spot = self.wait_spots.pop(0) if self.wait_spots else 0
        wx, wz = -27.0 - 5.5 * (spot // 7), -1.0 + 4.6 * (spot % 7)
        a.x, a.z = -60.0, wz
        yield from self.travel_truck(a, [(-60.0, wz), (wx, wz)], "arrive")
        self.tlog(tr, "gate")
        late = (tr.arrival - tr.sched) / 60
        self.note(
            f"{self.hhmm(env.now)} Truck {tr.tid} ({'inbound' if tr.kind == 'IN' else 'outbound'}) arrived "
            f"{'+' if late >= 0 else ''}{late:.0f} min vs plan"
        )
        tr.door = yield self.doors.get()
        self.wait_spots.insert(0, spot)
        self.wait_spots.sort()
        dx, dz = DOORS[tr.door]
        tr.t_dock = env.now
        yield from self.travel_truck(
            a, [(wx, wz), (-24.0, wz), (-24.0, dz), (TRUCK_X, dz)], "dock"
        )
        self.tlog(tr, "docked")
        if tr.kind == "IN":
            procs = [env.process(self.unload_pallet(tr, p)) for p in tr.pallets]
            if procs:
                yield env.all_of(procs)
            ck = yield from self.grab(self.checkers)
            yield from self.travel([ck], self.door_pt(tr.door), c.walk, "walk")
            yield from self.act([ck], c.t_doc_check, "check")
            self.release(self.checkers, ck)
        else:
            tr.ship.docked.succeed()
            yield tr.ship.loaded
        tr.t_done = env.now
        self.tlog(tr, "leaving")
        yield from self.travel_truck(
            a, [(TRUCK_X, dz), (-24.0, dz), (-24.0, -8.0), (-64.0, -8.0)], "leave"
        )
        tr.t_leave = env.now
        self.tlog(tr, "gone")
        self.doors.put(tr.door)

    def travel_truck(self, a, pts, label):
        dist = _plen(pts)
        dur = dist / self.cfg.truck_speed
        t = self.env.now
        a.segs.append(
            [
                round(t, 1),
                round(t + dur, 1),
                [(round(x, 2), round(z, 2)) for x, z in pts],
                label,
                0,
            ]
        )
        yield self.env.timeout(dur)
        a.x, a.z = pts[-1]

    # ---- monitor / main ----
    def sample(self):
        t = self.env.now
        gate = sum(
            1 for tr in self.trucks if tr.t_gate is not None and tr.t_dock is None
        )
        self.log["series"].append(
            dict(
                t=round(t),
                gate=gate,
                doors=self.cfg.dock_doors - len(self.doors.items),
                q_forklift=self.forklifts.queue,
                q_stacker=self.stackers.queue,
                q_picker=self.pickers.queue,
                q_checker=self.checkers.queue,
                q_loader=self.loaders.queue,
                recv_used=self.cfg.recv_spots - len(self.recv.items),
                lanes_used=self.cfg.staging_lanes - len(self.lanes.items),
                recv=self.stats["recv"],
                stored=self.stats["stored"],
                shipped=self.stats["shipped"],
                picked=self.stats["picked_cartons"],
                replen=self.stats["replen"] + self.stats["reloc"],
                busy={
                    p.role: sum(
                        a.busy + (self.env.now - a._t0 if self._is_busy(p, a) else 0)
                        for a in p.agents
                    )
                    for p in self.pools
                },
            )
        )

    def monitor(self):
        while not self.finished:
            self.sample()
            yield self.env.timeout(60)

    def _is_busy(self, pool, a):
        return a not in pool.store.items

    def main(self):
        env = self.env
        self.trucks = self.make_trucks()
        for tr in self.trucks:
            tr.agent = Agent(f"T{tr.tid}", "truck", (0, -40, FRONT))
        ships = [t.ship for t in self.trucks if t.ship]
        env.process(self.monitor())
        env.process(self.allocate_shipments(ships))
        for sh in ships:
            env.process(self.shipment_proc(sh))
        procs = [env.process(self.truck_proc(tr)) for tr in self.trucks]
        if procs:
            yield env.all_of(procs)
        while True:
            alive = [p for p in self.bg if p.is_alive]
            if not alive:
                break
            yield env.all_of(alive)
        self.finished = True
        self.note(f"{self.hhmm(env.now)} all trucks gone and all pallets stored")

    def run(self):
        proc = self.env.process(self.main())
        self.env.run(until=proc)
        self.end = self.env.now
        if not self.log["series"]:  # no trucks at all: still one sample for the UI
            self.sample()
        return self


# --------------------------------------------------------------------------- reporting
def final_cartons(sim):
    tot = 0
    seen = set()
    for ps in sim.sku_pallets.values():
        for p in ps:
            if id(p) not in seen:
                seen.add(id(p))
                tot += p.qty
    return tot


def kpis(sim: Sim):
    end = sim.end
    k = dict(finished_at=sim.hhmm(end), duration_h=round(end / 3600, 2), **sim.stats)
    k["utilisation_pct"] = {}
    k["avg_wait_min"] = {}
    k["p90_wait_min"] = {}
    for p in sim.pools:
        busy = sum(a.busy for a in p.agents)
        k["utilisation_pct"][p.role] = (
            round(100 * busy / (len(p.agents) * end), 1) if end else 0.0
        )
        w = sorted(p.waits) or [0]
        k["avg_wait_min"][p.role] = round(statistics.mean(w) / 60, 2)
        k["p90_wait_min"][p.role] = round(w[int(0.9 * (len(w) - 1))] / 60, 2)
    rows = []
    for t in sim.trucks:
        s = t.ship
        rows.append(
            dict(
                truck=t.tid,
                type=t.kind,
                planned=sim.hhmm(t.sched),
                arrived=sim.hhmm(t.arrival),
                late_min=round((t.arrival - t.sched) / 60),
                gate_wait_min=round((t.t_dock - t.t_gate) / 60, 1),
                at_dock_min=round((t.t_done - t.t_dock) / 60, 1),
                turnaround_min=round((t.t_leave - t.t_gate) / 60, 1),
                pallets=len(t.pallets)
                if t.kind == "IN"
                else (s.staged + (1 if s.cases_cartons else 0)),
                goods_ready=sim.hhmm(s.ready_t) if s and s.ready_t else "",
                staged_wait_min=round((t.t_dock - s.ready_t) / 60, 1)
                if s and s.ready_t and t.t_dock > s.ready_t
                else 0,
                short_cartons=sum(q for _, q in s.short) if s else 0,
                sid=s.sid if s else "",
                dest=s.dest if s else "",
                city=s.city if s else "",
                ttype=s.ttype if s else "",
            )
        )
    k["trucks"] = rows
    k["truck_plan"] = dict(getattr(sim, "truck_plan", {}))
    if sim.cfg.schedule_file:
        k["schedule"] = dict(
            file=Path(sim.cfg.schedule_file).name,
            shipments=getattr(sim, "n_real_ships", 0),
        )
    k["arrival_sequence"] = [t.tid for t in sorted(sim.trucks, key=lambda t: t.arrival)]
    k["stock_check"] = dict(
        initial=sim.initial_cartons,
        inbound=sim.inbound_cartons,
        outbound=sim.stats["picked_cartons"],
        final=final_cartons(sim),
        excluded_quarantine=sim.excluded_cartons,
    )
    k["stock_check"]["balanced"] = (
        k["stock_check"]["initial"]
        + k["stock_check"]["inbound"]
        - k["stock_check"]["outbound"]
        == k["stock_check"]["final"]
    )
    return k


def export(sim: Sim, outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    agents = {}
    for a in sim.agents + [t.agent for t in sim.trucks]:
        agents[a.name] = dict(
            role=a.role,
            home=[a.home[0], a.home[1]] if a.role != "truck" else [0, -40],
            segs=a.segs,
        )
    trucks = {
        t.tid: dict(
            kind=t.kind,
            sched=t.sched,
            arrival=t.arrival,
            states=t.states,
            pallets=len(t.pallets) or (t.ship.staged if t.ship else 0),
            door=t.door,
            sid=t.ship.sid if t.ship else "",
            dest=t.ship.dest if t.ship else "",
            city=t.ship.city if t.ship else "",
        )
        for t in sim.trucks
    }
    bins = [
        [
            b.code,
            round(b.x, 3),
            round(b.y, 2),
            round(b.z, 3),
            b.lane,
            b.face,
            b.lv,
            b.bay,
            b.pos,
        ]
        for b in sim.bin_list
    ]
    log = dict(
        meta=dict(
            t0=sim.t0,
            end=sim.end,
            layout=LAYOUT,
            pitch=PITCH,
            doors=DOORS,
            gates_total=len(DOORS),
            truck_x=TRUCK_X,
            recv=[RECV_X, RECV_Z0, RECV_DZ],
            staging_x=STAGING_X,
            lanes=sim.cfg.staging_lanes,
            recv_spots=sim.cfg.recv_spots,
            config=asdict(sim.cfg),
            truck_plan=getattr(sim, "truck_plan", {}),
            desk=DESK[:2],
        ),
        bins=bins,
        initial_state=sim.initial_state,
        pickfaces=sim.initial_pickfaces,
        bin_ev=sim.log["bin_ev"],
        recv_ev=sim.log["recv_ev"],
        lane_ev=sim.log["lane_ev"],
        series=sim.log["series"],
        notes=sim.log["notes"],
        agents=agents,
        trucks=trucks,
    )
    (outdir / "sim_log.json").write_text(json.dumps(log, separators=(",", ":")))
    k = kpis(sim)
    (outdir / "kpis.json").write_text(json.dumps(k, indent=2))
    with open(outdir / "trucks.csv", "w", newline="") as f:
        if not k["trucks"]:
            f.write("truck,type,planned,arrived\n")
            return k
        w = csv.DictWriter(f, fieldnames=list(k["trucks"][0].keys()))
        w.writeheader()
        w.writerows(k["trucks"])
    return k


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    d = Config()
    ap.add_argument("--out", default="out")
    ap.add_argument("--seed", type=int, default=d.seed)
    ap.add_argument("--trucks", type=int, default=d.n_trucks)
    ap.add_argument(
        "--trucks-in",
        type=int,
        default=d.n_trucks_in,
        help="inbound trucks today; -1 = derive from --trucks",
    )
    ap.add_argument(
        "--trucks-out",
        type=int,
        default=d.n_trucks_out,
        help="outbound trucks today, earliest scheduled shipments first; "
        "-1 = every scheduled shipment",
    )
    ap.add_argument("--pickers", type=int, default=d.n_pickers)
    ap.add_argument("--checkers", type=int, default=d.n_checkers)
    ap.add_argument("--stackers", type=int, default=d.n_stackers)
    ap.add_argument("--forklifts", type=int, default=d.n_forklifts)
    ap.add_argument("--loaders", type=int, default=d.n_loaders)
    ap.add_argument("--docks", type=int, default=d.dock_doors)
    ap.add_argument("--load-factor", type=float, default=d.load_factor)
    ap.add_argument(
        "--arrival-order",
        default="",
        help="e.g. 2,4,6,1,7,9 - these trucks arrive first, in this order",
    )
    ap.add_argument(
        "--schedule",
        default="",
        help="data/schedule.json (from extract_schedule.py): real outbound orders "
        "from the 'Schedule of the day' sheet; empty = auto (default file if present)",
    )
    ap.add_argument(
        "--no-schedule",
        action="store_true",
        help="ignore the schedule file and use the synthetic truck plan",
    )
    ap.add_argument(
        "--overrides",
        default="",
        help='JSON of Config overrides, e.g. \'{"t_check_pallet":120,"n_pickers":3,"dock_doors":3}\'',
    )
    a = ap.parse_args()
    sched = a.schedule
    if not sched and not a.no_schedule:
        dflt = Path(__file__).resolve().parent / "data" / "schedule.json"
        sched = str(dflt) if dflt.exists() else ""
    if a.no_schedule:
        sched = ""
    cfg = Config(
        seed=a.seed,
        n_trucks=a.trucks,
        n_trucks_in=a.trucks_in,
        n_trucks_out=a.trucks_out,
        n_pickers=a.pickers,
        n_checkers=a.checkers,
        n_stackers=a.stackers,
        n_forklifts=a.forklifts,
        n_loaders=a.loaders,
        dock_doors=a.docks,
        load_factor=a.load_factor,
        arrival_order=tuple(int(x) for x in a.arrival_order.split(",") if x.strip()),
        schedule_file=sched,
    )
    if a.overrides:
        ov = json.loads(a.overrides)
        if not isinstance(ov, dict):
            raise SystemExit("--overrides must be a JSON object")
        for k, v in ov.items():
            if k not in Config.__dataclass_fields__:
                raise SystemExit(f"--overrides: unknown config key {k!r}")
            cur = getattr(cfg, k)
            if isinstance(cur, bool):
                v = bool(v)
            elif isinstance(cur, int):
                v = int(v)
            elif isinstance(cur, float):
                v = float(v)
            elif isinstance(cur, str):
                v = str(v)
            else:  # tuple
                v = tuple(v)
            setattr(cfg, k, v)
        print("overrides applied:", ", ".join(f"{k}={getattr(cfg, k)}" for k in ov))
    sim = Sim(cfg).run()
    k = export(sim, Path(a.out))
    print(
        f"Simulated {k['duration_h']} h (finished {k['finished_at']}).  Truck arrival order: {k['arrival_sequence']}"
    )
    print("utilisation %:", k["utilisation_pct"])
    print("stock check:", k["stock_check"])
    for r in k["trucks"]:
        print(r)


if __name__ == "__main__":
    main()
