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
    Inbound truck : gate queue -> checker gate check (docs + pallets, rule 5)
                    -> dock door -> ONE operator + ONE vehicle (forklift
                    preferred, reach allowed) unloads each pallet to receiving;
                    a loader may assist (halves per-pallet time, [ASSUMPTION]).
    Outbound truck: day-start allocation (FEFO, same rules as docs/ALLOCATOR.md)
                    -> operator+vehicle pallet picks + picker hand picks into a
                    staging lane -> checkers audit lines -> wait for the (late)
                    truck -> loading by `loaders_per_truck` loaders AND one
                    operator+vehicle, whole-truck time bounded to 15-20 min
                    (grows with the load) -> truck leaves.
    Replenishment : operator+vehicle (reach truck above level A) moves leftovers
                    of opened pallets and refills empty pickfaces (bin-to-bin).
    Breaks        : two shifts 08:00-21:00; break1 11:45-13:00, break2
                    17:00-18:00 (config). Staff on an unfinished break never
                    START a new task; a held task finishes first (assumption e).

    Outbound orders: with --schedule data/schedule.json (from extract_schedule.py)
    the outbound trucks are the REAL shipments of the "Schedule of the day" sheet
    (arrival slots, customer, truck type); lateness is still simulated.

    Truck plan: --trucks-out N runs the N earliest scheduled shipments (extra
    trucks get synthetic orders); --trucks-in N sets the inbound trucks, spread
    through the morning.  Leaving both unset keeps --trucks as the day's total
    (every scheduled shipment + the remainder inbound).

Pickface rule (docs/ALLOCATOR.md): any quantity < UPP (partial put-away,
    leftover of an opened pallet, bin-to-bin remainder) goes into the SKU's
    pickface when free, else the nearest empty level-A bin, else it stays put
    (retried every t_reassign_retry).  Full pallets: pickface if free, else
    'BCDE' reserve (upstream behaviour).

Staff (rule 2): 2 operators (op1, op2) each driving ONE vehicle at a time — every
vehicle task holds one operator + one vehicle (op_veh/op_veh_release); 2
forklifts (F1,F2) + 2 reach trucks (R1,R2) as vehicles; checker/picker/loader
pools ch*/pk*/ld*.  No restocker or inventory-controller pool.
Vehicles used: reach for rack levels B-E (stacker_min_lv=1), forklift for
level A/floor, either type at the dock (forklift preferred).

Run:  python wms_sim.py --out out
Tests: python tests/test_fefo.py  (layout + FEFO, untouched)
       python tests/test_ops.py   (breaks, one-vehicle-per-operator,
                                   pickface rule, 15-20 min loading, balance)
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
        self.starts = []  # wall times when a task was acquired (new task starts)
        self.busy = 0.0
        self._t0 = 0.0

    def pos(self):
        return (self.x, self.z, self.lane)


class Pool:
    """Staff or vehicle pool.

    FilterStore: `grab()` only hands out agents that are NOT on break, so a
    waiting task can never start while its worker has a break due."""

    def __init__(self, env, role, n, home):
        self.role = role
        self.store = simpy.FilterStore(env)
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
    helper_on: bool = False  # a loader is assisting the inbound unload
    unloaded: object = None  # event: all inbound pallets off the truck
    load_min: float = None  # outbound: bounded whole-truck loading time (min)
    loaders_used: int = 0  # outbound: loaders attached to the load


# --------------------------------------------------------------------------- configuration
@dataclass
class Config:
    seed: int = 7
    start_hhmm: str = "08:00"
    # staff and equipment (from the request)
    n_pickers: int = 2
    n_checkers: int = 2
    n_operators: int = 2  # op1+op2, each driving ONE vehicle per task
    n_stackers: int = 2  # reach trucks, put away / pick from the TOP levels
    n_forklifts: int = 2  # low levels and floor moves
    n_loaders: int = 4  # pemuat
    stacker_min_lv: int = (
        1  # level index (0=A .. 4=E): >= this is reach-truck work (B-E, [ASSUMPTION])
    )
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

    # ASSUMPTIONS TO CONFIRM (guesses; adjust via --overrides or Config())
    # - Shift assignments per role (1=shift1 08-17, 2=shift2 13-21, 3=all-day 08-21):
    #   operators: [1,2], checkers: [1,2], loaders: [1,1,2,2], pickers: [3,3]
    # - Truck capacities by type: {"CDD":6,"CDE":10,"WBX":16,"C20":8,"GRM":10,"TRAGA":14,"default":12}
    # - Loaders per truck: 2; loaders also unload inbound; load_operator_vehicle=True
    # - Load times: t_load_base_min=15, t_load_min_min=15, t_load_max_min=20
    # - Reassignment retry window: t_reassign_retry=600s
    shift1: tuple = ("08:00", "17:00")
    shift2: tuple = ("13:00", "21:00")
    break1: tuple = ("11:45", "13:00")
    break2: tuple = ("17:00", "18:00")
    staff_duty: dict = field(
        default_factory=lambda: {
            "operator": [1, 2],
            "checker": [1, 2],
            "loader": [1, 1, 2, 2],
            "picker": [3, 3],
        }
    )
    loaders_unload_inbound: bool = True
    loaders_per_truck: int = 2
    load_operator_vehicle: bool = True
    t_load_base_min: float = 15.0
    t_load_min_min: float = 15.0
    t_load_max_min: float = 20.0
    truck_capacity: dict = field(
        default_factory=lambda: {
            "CDD": 6,
            "CDE": 10,
            "WBX": 16,
            "C20": 8,
            "GRM": 10,
            "TRAGA": 14,
            "default": 12,
        }
    )
    t_reassign_retry: float = 600.0


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
        self._assign_duty_and_breaks()
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
            break_wait_tasks=0,
        )
        self.bg = []
        self.finished = False
        self.breaks_log = []
        self.veh_segs = {}  # {veh_name: [[t0, t1, op_name, label], ...]}

    # ---- world ----
    @staticmethod
    def hhmm_to_sec(hhmm: str) -> int:
        """Parse 'HH:MM' to seconds since midnight."""
        h, m = hhmm.split(":")
        return int(h) * 3600 + int(m) * 60

    @staticmethod
    def _clock(hhmm: str) -> int:
        return Sim.hhmm_to_sec(hhmm)

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
        self.operators = Pool(env, "operator", c.n_operators, (-4.0, -11.0, FRONT))
        self.pickers = Pool(env, "picker", c.n_pickers, HOME)
        self.checkers = Pool(env, "checker", c.n_checkers, HOME)
        self.stackers = Pool(env, "reach", c.n_stackers, (-7.0, -12.0, FRONT))
        self.forklifts = Pool(env, "forklift", c.n_forklifts, (-4.0, -11.0, FRONT))
        self.loaders = Pool(env, "loader", c.n_loaders, HOME)

        # Name agents per the real org chart: 2 operators drive all vehicles;
        # F*/R1 are the forklift/reach-truck *vehicles*, not staff.
        for i, a in enumerate(self.operators.agents):
            a.name = f"op{i + 1}"
        for i, a in enumerate(self.checkers.agents):
            a.name = f"ch{i + 1}"
        for i, a in enumerate(self.pickers.agents):
            a.name = f"pk{i + 1}"
        for i, a in enumerate(self.loaders.agents):
            a.name = f"ld{i + 1}"
        for i, a in enumerate(self.forklifts.agents):
            a.name = f"F{i + 1}"
        for i, a in enumerate(self.stackers.agents):
            a.name = f"R{i + 1}"

        self.pools = [
            self.operators,
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

    def _assign_duty_and_breaks(self):
        """Assign duty windows and break windows to each staff agent based on Config.

        Shift definitions (seconds since day start at self.t0):
          shift1: 08:00-17:00
          shift2: 13:00-21:00
          break1: 11:45-13:00 (within shift1)
          break2: 17:00-18:00 (within shift2)

        staff_duty maps role -> list of shift indices per agent (1=shift1, 2=shift2, 3=all-day).
        All-day agents (3) get BOTH breaks. Agents get only the break(s) that fall within their duty window.
        An agent keeps working past duty end only if mid-task (no auto-shutdown logic).
        """
        c = self.cfg

        # Convert shift/break HH:MM to seconds since self.t0
        def to_rel(hhmm: str) -> int:
            return self.hhmm_to_sec(hhmm) - self.t0

        s1_start, s1_end = to_rel(c.shift1[0]), to_rel(c.shift1[1])
        s2_start, s2_end = to_rel(c.shift2[0]), to_rel(c.shift2[1])
        b1_start, b1_end = to_rel(c.break1[0]), to_rel(c.break1[1])
        b2_start, b2_end = to_rel(c.break2[0]), to_rel(c.break2[1])

        shifts = {
            1: (s1_start, s1_end),
            2: (s2_start, s2_end),
            3: (s1_start, s2_end),  # all-day: 08:00-21:00
        }
        breaks = [
            (b1_start, b1_end),  # break1
            (b2_start, b2_end),  # break2
        ]

        # Map pool role names to staff_duty keys (vehicles take no breaks)
        role_to_duty_key = {
            "operator": "operator",
            "picker": "picker",
            "checker": "checker",
            "loader": "loader",
        }

        # First pass: initialize all agents with defaults
        for pool in self.pools:
            for agent in pool.agents:
                agent.on_break = False
                agent.duty = (0, float("inf"))
                agent.breaks = []

        # Second pass: assign duty/breaks for mapped roles
        for pool in self.pools:
            duty_key = role_to_duty_key.get(pool.role)
            if not duty_key:
                continue
            duty_list = c.staff_duty.get(duty_key, [])
            for i, agent in enumerate(pool.agents):
                shift_idx = duty_list[i] if i < len(duty_list) else 1
                d0, d1 = shifts.get(shift_idx, shifts[1])
                agent.duty = (d0, d1)
                # Assign breaks that intersect with duty window
                agent.breaks = []
                for b_start, b_end in breaks:
                    # Break must be fully within duty window (or at least overlap)
                    if b_end <= d0 or b_start >= d1:
                        continue
                    # Clamp break to duty window
                    br_start = max(b_start, d0)
                    br_end = min(b_end, d1)
                    if br_start < br_end:
                        agent.breaks.append((br_start, br_end))

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
        """Acquire a staff member or vehicle. Break-gated: only agents NOT on
        an unfinished break are handed out, so no new task can start during a
        break; a task that already holds its workers finishes (assumption e).
        Waits started while the pool was break-limited are counted."""
        t = self.env.now
        brk = [a for a in pool.agents if getattr(a, "on_break", False)]
        a = yield pool.store.get(lambda x: not getattr(x, "on_break", False))
        pool.waits.append(self.env.now - t)
        if self.env.now > t and brk:
            self.stats["break_wait_tasks"] += 1
        a._t0 = self.env.now
        a.starts.append(round(self.env.now, 1))
        return a

    def release(self, pool, a):
        a.busy += self.env.now - a._t0
        pool.store.put(a)

    def op_veh(self, label, lv=None, dock=False):
        """One operator + one free vehicle of the right type for a task.

        A task needs exactly one operator plus one vehicle: an operator can
        never hold two vehicles, because both are granted together here and
        released together. Vehicles may sit idle while operators are the limit.
        Reach trucks serve levels B-E (stacker_min_lv); dock work (truck
        load/unload) prefers a free forklift, either type serves."""
        op = yield from self.grab(self.operators)
        pool = self.veh(lv, dock)
        fk = yield from self.grab(pool)
        ent = [round(self.env.now, 1), None, op.name, label]
        self.veh_segs.setdefault(fk.name, []).append(ent)
        return op, fk, pool, ent

    def op_veh_release(self, pool, op, fk, ent):
        ent[1] = round(self.env.now, 1)
        self.release(pool, fk)
        self.release(self.operators, op)

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
    def veh(self, lv=None, dock=False):
        """Vehicle pool for a move.

        - Reach trucks (self.stackers) serve rack levels B-E: put-away above
          level A, picks from upper levels, lowering a pallet (dock=False and
          lv >= stacker_min_lv).
        - Forklifts serve level A / floor; dock work prefers a free forklift
          but either type serves (dock=True)."""
        if dock:
            return self.forklifts if self.forklifts.store.items else self.stackers
        if lv is not None and lv >= self.cfg.stacker_min_lv:
            return self.stackers
        return self.forklifts

    def pick_up(self, p, veh):
        """The vehicle takes a pallet out of its bin (or off the floor)."""
        b = p.bin
        if b is not None:
            yield from self.act(veh, self.lift_time(b.lv), "lift")
            b.pallet = None
            p.loc = b.access()
            p.bin = None
            self.bin_ev(b, 3)
        else:
            yield from self.act(veh, self.cfg.t_fork_pos, "lift")

    def put_down(self, p, b, veh):
        yield from self.act(veh, self.lift_time(b.lv), "place")
        b.pallet = p
        b.claim = None
        p.bin, p.loc = b, None
        self.bin_ev(b, 3)

    def carry_job(self, p, target, label):
        """One operator + one vehicle move pallet p into `target` bin.

        The vehicle carries the pallet (reach truck above level A
        stacker_min_lv, forklift below); the operator rides/drives it for the
        whole move, so an operator holds at most one vehicle at a time."""
        op, fk, pool, ent = yield from self.op_veh(label, lv=target.lv)
        cons = [op, fk]
        yield from self.travel(cons, p.access(), self.cfg.fork_empty, "to_pallet")
        with p.lock.request() as rq:
            yield rq
            yield from self.pick_up(p, cons)
            yield from self.travel(
                cons, target.access(), self.cfg.fork_loaded, label, carry=1
            )
            yield from self.put_down(p, target, cons)
        self.op_veh_release(pool, op, fk, ent)

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
        """Bin-to-bin: leftover of an opened pallet goes to the SKU's pickface
        (if free) or the nearest empty level-A bin (overflow). With no free
        level-A slot it stays put (docs/ALLOCATOR.md: 'tetap di bin')."""
        pf = self.pickface.get(p.sku)
        if pf and pf.pallet is None and pf.claim is None:
            tgt = pf
        else:
            tgt = self.nearest_empty(p.access(), "A")
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
    def putaway_target(self, sku, qty, from_pt=None):
        """Pure pickface rule (docs/ALLOCATOR.md): any quantity less than UPP
        never enters a reserve pallet bin — it goes into the SKU's pickface
        when that is free, else the nearest empty level-A slot, else None
        (goods stay put and a retry re-plans them). Full pallets (qty >= UPP)
        keep the pickface-if-free then reserve 'BCDE' preference."""
        upp = self.items[sku].upp
        pf = self.pickface.get(sku)
        pt = from_pt if from_pt is not None else (0.0, 0.0, FRONT)
        free = pf is not None and pf.pallet is None and pf.claim is None
        if qty < upp:
            if free:
                return pf
            return self.nearest_empty(pt, "A")
        if free:
            return pf
        return self.nearest_empty(pt, "BCDE") or self.nearest_empty(pt)

    def receive_pallet(self, truck, p, spot):
        """Put-away of a received pallet by one operator + one vehicle
        (checkers sit at the gate; they do not assign bins or drive)."""
        c = self.cfg
        p.loc = self.recv_pt(spot)
        tgt = self.putaway_target(p.sku, p.qty, p.loc)
        if tgt is None:  # partial with no open slot: goods stay in receiving
            self.note(
                f"{self.hhmm(self.env.now)} {p.sku} x{p.qty}"
                f" stays in recv: no free level-A slot yet"
            )
            for _ in range(12):  # ~2h max of 'stays put', then overflow once
                yield self.env.timeout(c.t_reassign_retry)
                amt = self.putaway_target(p.sku, p.qty, p.loc)
                if amt is not None:
                    break
            else:
                amt = self.nearest_empty(p.loc)  # last resort: any reserve slot
                if amt is not None:
                    self.note(
                        f"{self.hhmm(self.env.now)} {p.sku} x{p.qty}"
                        f" overflowed to {amt.code} (no level-A slot)"
                    )
            if amt is None:
                self.note(
                    f"{self.hhmm(self.env.now)} {p.sku} x{p.qty}"
                    f" still in recv: warehouse full - giving up"
                )
                self.log["recv_ev"].append([round(self.env.now, 1), spot, 0])
                self.recv.put(spot)
                return
            tgt = amt
        tgt.claim = p
        op, fk, pool, ent = yield from self.op_veh(
            f"putaway {p.sku} x{p.qty}", lv=tgt.lv
        )
        cons = [op, fk]
        yield from self.act(cons, c.t_assign, "assign")  # desk/cabin step
        yield from self.travel(cons, p.access(), c.fork_empty, "to_pallet")
        with p.lock.request() as rq:
            yield rq
            yield from self.pick_up(p, cons)
            yield from self.travel(cons, tgt.access(), c.fork_loaded, "putaway", 1)
            yield from self.put_down(p, tgt, cons)
        self.op_veh_release(pool, op, fk, ent)
        self.log["recv_ev"].append([round(self.env.now, 1), spot, 0])
        self.recv.put(spot)
        self.sku_pallets[p.sku].add(p)
        self.stats["stored"] += 1

    def unload_pallet(self, truck, p):
        """Inbound unload: one operator + one vehicle (forklift preferred at the
        dock). With a loader helping out (assumption b, default yes) the
        per-pallet work halves: forklifts do the heavy pallets.

        The receiving spot is claimed BEFORE the crew, so truck backpressure
        never strands both operators as unloaders: put-away (same operator
        pool) can always progress and free spots."""
        c = self.cfg
        spot = yield self.recv.get()
        op, fk, pool, ent = yield from self.op_veh(
            f"unload {p.sku} T{truck.tid}", dock=True
        )
        cons = [op, fk]
        factor = 0.5 if getattr(truck, "helper_on", False) else 1.0
        yield from self.travel(cons, self.door_pt(truck.door), c.fork_empty, "to_truck")
        yield from self.act(cons, c.t_unload_pallet * factor, "unload")
        yield from self.travel(cons, self.recv_pt(spot), c.fork_loaded, "move", carry=1)
        yield from self.act(cons, c.t_fork_pos, "drop")
        p.loc = self.recv_pt(spot)
        self.op_veh_release(pool, op, fk, ent)
        self.log["recv_ev"].append([round(self.env.now, 1), spot, 1])
        self.stats["recv"] += 1
        self.spawn(self.receive_pallet(truck, p, spot))

    def loader_helper(self, tr):
        """One loader assists the whole unload of truck tr (assumption b)."""
        ld = yield from self.grab(self.loaders)
        yield from self.travel([ld], self.door_pt(tr.door), self.cfg.walk, "walk")
        yield from self.act([ld], 2, "assist_unload")
        yield tr.unloaded
        self.release(self.loaders, ld)

    # ---- outbound ----
    def fl_picklist(self, sh, tasks):
        c = self.cfg
        for t in sorted(tasks, key=lambda t: t.pallet.key()):
            p = t.pallet
            lv = p.bin.lv if p.bin else 0
            op, fk, pool, ent = yield from self.op_veh(f"pick {p.sku} x{t.take}", lv=lv)
            cons = [op, fk]
            yield from self.travel(cons, p.access(), c.fork_empty, "to_pallet")
            with p.lock.request() as rq:
                yield rq
                was_pf = p.bin is not None and self.pickface.get(p.sku) is p.bin
                yield from self.pick_up(p, cons)
                yield from self.travel(
                    cons,
                    self.lane_pt(sh.lane),
                    c.fork_loaded,
                    "to_staging",
                    carry=1,
                )
                yield from self.act(cons, c.t_drop, "drop")
                p.loc = self.lane_pt(sh.lane)
                self.sku_pallets[p.sku].discard(p)
                p.qty -= t.take
                p.reserved -= t.take
            self.op_veh_release(pool, op, fk, ent)
            sh.staged += 1
            self.stats["fl_picks"] += 1
            self.stats["picked_cartons"] += t.take
            self.log["lane_ev"].append(
                [round(self.env.now, 1), sh.lane, sh.staged, sh.truck]
            )
            if was_pf:
                self.maybe_replen(p.sku)

    def lower_pallet(self, p):
        """A pallet in a rack level B-E is brought down to the floor so a picker
        can open it (operator + reach truck)."""
        op, fk, pool, ent = yield from self.op_veh(
            "lower_pallet", lv=p.bin.lv if p.bin else 0
        )
        cons = [op, fk]
        yield from self.travel(cons, p.access(), self.cfg.fork_empty, "to_pallet")
        with p.lock.request() as rq:
            yield rq
            if p.bin is not None:
                yield from self.pick_up(p, cons)
                yield from self.act(cons, 8, "place")
        self.op_veh_release(pool, op, fk, ent)

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
        tr = self.trucks[sh.truck - 1]
        if n_load:
            yield from self.load_truck(sh, tr.door, tr, n_load)
        sh.loaded_t = env.now
        self.stats["shipped"] += n_load
        self.log["lane_ev"].append([round(env.now, 1), sh.lane, -1, sh.truck])
        self.lanes.put(sh.lane)
        sh.loaded.succeed()

    def load_min(self, n_units, ttype):
        """Whole-truck loading time, growing with the truck's load and bounded
        to [t_load_min_min, t_load_max_min] = 15..20 min (config)."""
        c = self.cfg
        cap = c.truck_capacity.get(ttype, c.truck_capacity.get("default", 12))
        mm = c.t_load_base_min + (c.t_load_max_min - c.t_load_base_min) * min(
            1.0, n_units / cap
        )
        return min(max(mm, c.t_load_min_min), c.t_load_max_min)

    def load_truck(self, sh, door, tr, n_units):
        """Outbound truck loading (rule 3+4).

        The 4 loaders load the trucks; a load is NOT a loaders-only task: it
        also holds one operator and one vehicle (forklift at the dock, reach if
        the forklift pool is idle-less) for the whole window. Elapsed time is
        bounded to 15-20 minutes, growing with pallets/cartons.
        """
        c = self.cfg
        mm = self.load_min(n_units, sh.ttype)
        t0 = self.env.now
        op, fk, pool, ent = yield from self.op_veh(
            f"load T{tr.tid} {sh.sid}", dock=True
        )
        lds = []
        for _ in range(c.loaders_per_truck):
            ld = yield from self.grab(self.loaders)
            lds.append(ld)
        team = [op, fk] + lds
        yield from self.travel(team, self.lane_pt(sh.lane), c.jack, "walk")
        yield from self.act(team, mm * 60.0, "load")
        yield from self.travel([op, fk], self.door_pt(door), c.jack, "to_dock", carry=1)
        yield from self.act([op, fk], 30, "load", carry=1)
        tr.load_min = round(mm, 2)
        tr.loaders_used = len(lds)
        for ld in lds:
            self.release(self.loaders, ld)
        self.op_veh_release(pool, op, fk, ent)

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
        # Gate check (rule 5): the truck waits at the gate until a checker is
        # free; checkers do not drive vehicles and do not do put-away.
        ck = yield from self.grab(self.checkers)
        yield from self.travel([ck], (wx, wz, FRONT), c.walk, "walk")
        gate_work = (
            c.t_doc_check + len(tr.pallets) * c.t_check_pallet
            if tr.kind == "IN"
            else c.t_doc_check
        )
        yield from self.act([ck], gate_work, "doc_check")
        self.release(self.checkers, ck)
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
            tr.helper_on = bool(c.loaders_unload_inbound) and any(
                not getattr(a2, "on_break", False) for a2 in self.loaders.agents
            )
            if tr.helper_on:
                self.spawn(self.loader_helper(tr))
            procs = [env.process(self.unload_pallet(tr, p)) for p in tr.pallets]
            if procs:
                yield env.all_of(procs)
            tr.unloaded.succeed()
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
        busy = {
            p.role: sum(
                a.busy + (self.env.now - a._t0 if self._is_busy(p, a) else 0)
                for a in p.agents
            )
            for p in self.pools
        }
        self.log["series"].append(
            dict(
                t=round(t),
                gate=gate,
                doors=self.cfg.dock_doors - len(self.doors.items),
                q_operator=self.operators.queue,
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
                busy=busy,
                busy_op=busy.get("operator", 0),
                busy_f=busy.get("forklift", 0),
                busy_r=busy.get("reach", 0),
            )
        )

    def monitor(self):
        while not self.finished:
            self.sample()
            yield self.env.timeout(60)

    def break_control(self):
        """Central process managing agent break flags.

        Steps through sorted boundaries of all break windows + day boundaries (08:00-21:00).
        At each boundary, updates on_break flags for agents whose duty covers the current time.
        Logs break start/end to self.breaks_log and self.log["notes"].

        Approximation (assumption e): at window start, only agents currently idle (in pool store)
        are marked on_break. Busy agents keep working and the window "burns" for them (not logged).
        At window end, all agents in that window have on_break set to False.
        """
        c = self.cfg
        # Collect all unique break boundaries across all agents
        boundaries = set()
        for a in self.agents:
            for b_start, b_end in getattr(a, "breaks", []):
                boundaries.add(b_start)
                boundaries.add(b_end)
            d0, d1 = getattr(a, "duty", (0, float("inf")))
            for d in (d0, d1):
                if math.isfinite(d):
                    boundaries.add(d)
        # Add day boundaries (08:00 and 21:00 relative to t0)
        day_start = self.hhmm_to_sec("08:00") - self.t0
        day_end = self.hhmm_to_sec("21:00") - self.t0
        boundaries.add(day_start)
        boundaries.add(day_end)

        sorted_bounds = sorted(boundaries)

        # For each interval between boundaries, determine which agents are on break
        for i in range(len(sorted_bounds) - 1):
            t0 = sorted_bounds[i]
            t1 = sorted_bounds[i + 1]
            if t0 >= t1:
                continue

            # Midpoint of interval to test break membership
            t_mid = (t0 + t1) / 2
            flags_before = [a.on_break for a in self.agents]

            # Find agents whose duty does not cover t_mid or whose break
            # window covers t_mid: an agent operating today but outside duty
            # (shift edges) is effectively off; agents on an intraday break
            # are on break. Both states are guided through the same
            # availability flag ('on_break' = not acquirable for NEW tasks).
            for a in self.agents:
                duty_start, duty_end = getattr(a, "duty", (0, float("inf")))
                on_duty = duty_start <= t_mid < duty_end
                in_break = any(
                    b_start <= t_mid < b_end
                    for b_start, b_end in getattr(a, "breaks", [])
                )

                if a.on_break and on_duty and not in_break:
                    # Duty started or break window ended: agent is available
                    # for new tasks again.
                    a.on_break = False
                    self.note(f"{self.hhmm(t0)} BREAK {a.name} off")
                elif not a.on_break and not on_duty:
                    # Shift edge: agent off today (a task already started may
                    # still run into it; the flag only gates NEW tasks).
                    a.on_break = True
                elif not a.on_break and in_break:
                    # Break window starts: every agent on duty goes on break.
                    # A task already in progress is unaffected (it holds the
                    # agent outside the store); when it finishes mid-window,
                    # the released agent is simply kept out of new tasks until
                    # the window ends, i.e. the break absorbs the remainder
                    # (assumption e default: he finishes the task first).
                    a.on_break = True
                    win = next(
                        (b for b in a.breaks if b[0] <= t_mid < b[1]),
                        (t0, t1),
                    )
                    self.breaks_log.append(
                        [a.name, a.role, round(win[0], 1), round(win[1], 1)]
                    )
                    self.note(f"{self.hhmm(t0)} BREAK {a.name} on")

            # Re-scan pending acquisition requests whenever a break flag
            # flipped: agents released while on break were rejected by the
            # store filter; once the flag is off, waiters that were (rightly)
            # waiting for a free worker must finally proceed (a store put only
            # re-triggers _do_get once, at the moment of the put).
            if flags_before != [a.on_break for a in self.agents]:
                for p in self.pools:
                    p.store._trigger_get(None)

            # Wait until next boundary
            if self.env.now < t1:
                yield self.env.timeout(t1 - self.env.now)

    def _is_busy(self, pool, a):
        return a not in pool.store.items

    def main(self):
        env = self.env
        self.trucks = self.make_trucks()
        for tr in self.trucks:
            tr.agent = Agent(f"T{tr.tid}", "truck", (0, -40, FRONT))
            tr.unloaded = env.event()
        ships = [t.ship for t in self.trucks if t.ship]
        env.process(self.monitor())
        env.process(self.break_control())
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
    k["operator_util_pct"] = k["utilisation_pct"].get("operator", 0.0)
    k["vehicle_util_pct"] = {
        "forklift": k["utilisation_pct"].get("forklift", 0.0),
        "reach": k["utilisation_pct"].get("reach", 0.0),
    }
    k["avg_wait_op_min"] = k["avg_wait_min"].get("operator", 0.0)
    k["p90_wait_op_min"] = k["p90_wait_min"].get("operator", 0.0)
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
                load_min=t.load_min if t.kind == "OUT" else None,
                loaders_used=t.loaders_used if t.kind == "OUT" else 0,
            )
        )
    k["trucks"] = rows
    by_type = {}
    for r in rows:
        tt = r["ttype"] or ("inbound" if r["type"] == "IN" else "unknown")
        by_type[tt] = by_type.get(tt, 0) + 1
    k["trucks_by_type"] = by_type
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
            task_starts=list(getattr(a, "starts", [])),
            duty=list(getattr(a, "duty", []))
            if not math.isinf(getattr(a, "duty", (0, float("inf")))[1])
            else None,
            breaks=[list(w) for w in getattr(a, "breaks", [])],
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
        breaks=sim.breaks_log,
        vehicle_segs={
            veh: [e if e[1] is not None else [e[0], sim.end, e[2], e[3]] for e in es]
            for veh, es in sim.veh_segs.items()
        },
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
