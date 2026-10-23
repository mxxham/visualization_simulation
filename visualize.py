#!/usr/bin/env python3
"""
Visualisation for wms_sim.py  (reads out/sim_log.json written by the simulation).

    python visualize.py --mode timelapse                 # whole day, mp4
    python visualize.py --mode clip --start 11:15 --duration 30 --speed 15
    python visualize.py --mode still --at 11:40          # one PNG frame
    python visualize.py --mode dashboard                 # KPI charts as PNG
    python visualize.py --mode live                      # interactive window (needs a display)
"""

from __future__ import annotations

import argparse
import json
import math
from bisect import bisect_right
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import animation
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle

# ----------------------------------------------------------------------------- palette
# Categorical slots of the reference palette (dataviz skill), assigned in fixed order to the roles.
INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#8a8984", "#e3e2de"
SURFACE, FLOOR = "#fcfcfb", "#f1f0ec"
ROLE_COLOR = {
    "picker": "#2a78d6",
    "loader": "#1baf7a",
    "checker": "#6250d6",
    "forklift": "#eda100",
    "stacker": "#e87ba4",
}
ROLE_MARKER = {
    "picker": "o",
    "loader": "s",
    "checker": "D",
    "forklift": "s",
    "stacker": "^",
}
ROLE_SIZE = {"picker": 7, "loader": 6, "checker": 6, "forklift": 11, "stacker": 8}
IN_COLOR, OUT_COLOR = "#2a78d6", "#eb6834"  # inbound / outbound trucks
BIN_EMPTY, BIN_FULL, BIN_PART, BIN_RESV = (
    (216, 218, 219),
    (31, 111, 139),
    (127, 184, 207),
    (224, 165, 38),
)
FLASH = {1: (209, 67, 47), 3: (142, 91, 199)}  # 1 = hand pick, 3 = forklift in/out
FLASH_S = 45.0
ROLE_N = {
    "picker": "n_pickers",
    "checker": "n_checkers",
    "stacker": "n_stackers",
    "forklift": "n_forklifts",
    "loader": "n_loaders",
}
ROLE_ORDER = ["picker", "checker", "loader", "forklift", "stacker"]


def hhmm(t0, t):
    s = int(t0 + t)
    return f"{s // 3600 % 24:02d}:{s // 60 % 60:02d}"


def parse_clock(txt, t0):
    h, m = txt.split(":")
    return int(h) * 3600 + int(m) * 60 - t0


# ----------------------------------------------------------------------------- tracks
class Track:
    """Position of one agent over time, interpolated along its recorded polylines."""

    def __init__(self, segs, home):
        self.segs = segs
        self.home = tuple(home)
        self.t0 = [s[0] for s in segs]
        self.cum = []
        for s in segs:
            pts = s[2]
            c = [0.0]
            for i in range(1, len(pts)):
                c.append(
                    c[-1]
                    + abs(pts[i][0] - pts[i - 1][0])
                    + abs(pts[i][1] - pts[i - 1][1])
                )
            self.cum.append(c)

    def at(self, t):
        """-> x, z, dx, dz, label, carry, active"""
        i = bisect_right(self.t0, t) - 1
        if i < 0:
            return self.home[0], self.home[1], 1, 0, "", 0, False
        t0, t1, pts, label, carry = self.segs[i]
        if t >= t1:
            x, z = pts[-1]
            dx = dz = 0
            if len(pts) > 1:
                dx, dz = pts[-1][0] - pts[-2][0], pts[-1][1] - pts[-2][1]
            return x, z, dx, dz, "", 0, False
        if len(pts) == 1:
            return pts[0][0], pts[0][1], 1, 0, label, carry, True
        c = self.cum[i]
        L = c[-1] * (t - t0) / (t1 - t0) if t1 > t0 else c[-1]
        j = max(1, bisect_right(c, L))
        j = min(j, len(pts) - 1)
        seg = c[j] - c[j - 1]
        f = (L - c[j - 1]) / seg if seg > 0 else 1.0
        x = pts[j - 1][0] + f * (pts[j][0] - pts[j - 1][0])
        z = pts[j - 1][1] + f * (pts[j][1] - pts[j - 1][1])
        return (
            x,
            z,
            pts[j][0] - pts[j - 1][0],
            pts[j][1] - pts[j - 1][1],
            label,
            carry,
            True,
        )


# ----------------------------------------------------------------------------- renderer
class Renderer:
    RES = 0.0715
    X0, Z0, X1, Z1 = -0.3, -1.5, 54.3, 29.5

    def __init__(self, log, figsize=(16, 9), dpi=120):
        self.log = log
        self.meta = log["meta"]
        self.t0 = self.meta["t0"]
        self.cfg = self.meta["config"]
        self.bins = log["bins"]
        self.series = log["series"]
        self.fig = plt.figure(figsize=figsize, dpi=dpi, facecolor=SURFACE)
        self.tracks = {n: Track(a["segs"], a["home"]) for n, a in log["agents"].items()}
        self.roles = {n: a["role"] for n, a in log["agents"].items()}
        self.build_map()
        self.build_hud()
        self.reset(0)

    # ---- static map ----
    def build_map(self):
        m = self.meta
        ax = self.ax = self.fig.add_axes([0.004, 0.315, 0.992, 0.672], facecolor=FLOOR)
        ax.set_xlim(-62, 66)
        ax.set_ylim(-6, 33)
        ax.set_aspect("equal", adjustable="box")
        for s in ax.spines.values():
            s.set_visible(False)
        ax.set_xticks([])
        ax.set_yticks([])
        # raster of rack bins (5 level strips per face, level A on the aisle side)
        R = self.RES
        self.W = int(round((self.X1 - self.X0) / R))
        self.H = int(round((self.Z1 - self.Z0) / R))
        self.img = np.zeros((self.H, self.W, 3), np.uint8)
        self.img[:] = (241, 240, 236)
        pitch = m["pitch"]
        depth = m["layout"]["rack_depth_m"]
        strip = depth / 5
        self.slice = []
        for code, x, y, z, lane, face, lv, bay, pos in self.bins:
            xl, xr = x - 0.675, x + 0.675
            if face == 0:
                zlo = z - depth / 2 + lv * strip
            else:
                zlo = z + depth / 2 - (lv + 1) * strip
            c0, c1 = int((xl - self.X0) / R) + 1, int((xr - self.X0) / R)
            r0, r1 = int((zlo - self.Z0) / R) + 1, int((zlo + strip - self.Z0) / R)
            self.slice.append((r0, r1, c0, c1))
        self.im = ax.imshow(
            self.img,
            extent=[self.X0, self.X0 + self.W * R, self.Z0, self.Z0 + self.H * R],
            origin="lower",
            interpolation="nearest",
            zorder=1,
        )
        # lanes and block labels
        for i, name in enumerate(["CA", "CB", "CC", "CD", "CE", "CF"]):
            zc = i * pitch
            ax.text(
                -1.4,
                zc,
                name,
                ha="right",
                va="center",
                fontsize=9,
                fontweight="bold",
                color=INK2,
                zorder=3,
            )
        for lane in range(7):
            zc = -pitch / 2 + pitch * lane
            ax.plot(
                [-2.5, 56.5],
                [zc, zc],
                color="#e4e3dd",
                lw=0.6,
                ls=(0, (2, 4)),
                zorder=0.5,
            )
        ax.plot(
            [-2.5, -2.5],
            [-2.8, 30.8],
            color="#e4e3dd",
            lw=0.6,
            ls=(0, (2, 4)),
            zorder=0.5,
        )
        ax.plot(
            [56.5, 56.5],
            [-2.8, 30.8],
            color="#e4e3dd",
            lw=0.6,
            ls=(0, (2, 4)),
            zorder=0.5,
        )
        ax.text(
            27,
            31.8,
            "Racking CA-CF  |  5 levels per bay, level A (ground) on the aisle side of each strip  |  gates on the west wall (left), bays run east",
            ha="center",
            fontsize=7.5,
            color=MUTED,
            zorder=3,
        )
        # pickface markers
        for sku, code in self.log["pickfaces"].items():
            pass
        # building wall + dock gates (G1..G7 on the short west wall, first cfg['dock_doors'] active)
        ax.plot([-13.5, -13.5], [-4.5, 31.5], color="#b9b8b2", lw=1.2, zorder=2)
        for i, (dx, dz) in enumerate(m["doors"]):
            active = i < self.cfg["dock_doors"]
            fc = "#cfe7df" if active else "#d6d5cf"
            ec = "#1baf7a" if active else "#8a8984"
            ax.add_patch(
                Rectangle(
                    (dx - 2.2, dz - 1.25), 2.7, 2.5, fc=fc, ec=ec, lw=0.8, zorder=2
                )
            )
            ax.text(
                -13.2,
                dz,
                f"G{i + 1}",
                ha="left",
                va="center",
                fontsize=6.5,
                fontweight="bold" if active else "normal",
                color=INK2 if active else MUTED,
                zorder=3,
            )
        ax.text(
            -58.5,
            30.6,
            f"TRUCK YARD  |  {len(m['doors'])} gates on the west wall, {self.cfg['dock_doors']} active (green)",
            fontsize=7,
            color=MUTED,
            ha="left",
            va="top",
        )
        ax.text(
            -58.5,
            29.4,
            "gate queue (trucks wait here for a free gate)",
            fontsize=7,
            color=MUTED,
            ha="left",
            va="top",
        )
        # outbound staging lanes (west floor, lanes run along z)
        self.lane_rect, self.lane_txt = [], []
        for i in range(m["lanes"]):
            z = (i + 2) * 1.4
            r = Rectangle(
                (-9.2, z - 0.6), 2.6, 1.2, fc="#ffffff", ec="#8a8984", lw=0.7, zorder=2
            )
            ax.add_patch(r)
            self.lane_rect.append(r)
            ax.text(
                -5.9,
                z,
                f"{i + 1}",
                ha="left",
                va="center",
                fontsize=5.5,
                color=MUTED,
                zorder=3,
            )
            self.lane_txt.append(
                ax.text(
                    -7.9,
                    z,
                    "",
                    ha="center",
                    va="center",
                    fontsize=6.5,
                    color="white",
                    fontweight="bold",
                    zorder=4,
                )
            )
        ax.text(
            -9.2,
            1.4,
            "OUTBOUND STAGING (STG_01-08)",
            fontsize=6.5,
            color=INK2,
            ha="left",
            va="bottom",
        )
        # receiving area (spots along z, west of the racks)
        rx, rz0, rdz = m["recv"]
        self.recv_rect = []
        for i in range(m["recv_spots"]):
            z = rz0 + rdz * i
            r = Rectangle(
                (rx - 0.55, z - 0.45),
                1.1,
                0.9,
                fc="#ffffff",
                ec="#b9b8b2",
                lw=0.6,
                zorder=2,
            )
            ax.add_patch(r)
            self.recv_rect.append(r)
        ax.text(
            rx - 0.7,
            rz0 + rdz * (m["recv_spots"] - 1) + 1.2,
            "RECEIVING AREA (16 spots)",
            fontsize=6.5,
            color=INK2,
            ha="left",
            va="bottom",
        )
        # trucks
        self.truck_patch, self.truck_txt = {}, {}
        for tid, tr in self.log["trucks"].items():
            col = IN_COLOR if tr["kind"] == "IN" else OUT_COLOR
            p = Rectangle(
                (0, 0),
                3.2,
                8.5,
                fc=col,
                ec=INK,
                lw=0.8,
                zorder=5,
                alpha=0.95,
                visible=False,
            )
            ax.add_patch(p)
            t = ax.text(
                0,
                0,
                "",
                ha="center",
                va="center",
                fontsize=6.5,
                color="white",
                fontweight="bold",
                zorder=6,
                visible=False,
            )
            self.truck_patch[int(tid)], self.truck_txt[int(tid)] = p, t
        # agents
        self.agent_art, self.carry_art = {}, {}
        for name, role in self.roles.items():
            if role == "truck":
                continue
            (ln,) = ax.plot(
                [],
                [],
                marker=ROLE_MARKER[role],
                ms=ROLE_SIZE[role],
                color=ROLE_COLOR[role],
                mec="white",
                mew=0.6,
                ls="",
                zorder=8 if role != "forklift" else 7,
            )
            self.agent_art[name] = ln
            (cr,) = ax.plot(
                [],
                [],
                marker="s",
                ms=4.5,
                color="#a9784a",
                mec=INK,
                mew=0.4,
                ls="",
                zorder=9,
            )
            self.carry_art[name] = cr
        # legend (map)
        hs = []
        for role in ROLE_ORDER:
            hs.append(
                Line2D(
                    [],
                    [],
                    marker=ROLE_MARKER[role],
                    ms=7,
                    color=ROLE_COLOR[role],
                    mec="white",
                    ls="",
                    label=f"{role} ({self.cfg[ROLE_N[role]]})",
                )
            )
        hs.append(
            Line2D(
                [],
                [],
                marker="s",
                ms=6,
                color="#a9784a",
                mec=INK,
                mew=0.4,
                ls="",
                label="pallet / cartons carried",
            )
        )
        ax.legend(
            handles=hs,
            loc="lower left",
            bbox_to_anchor=(0.0, 0.0),
            fontsize=6.3,
            frameon=True,
            framealpha=0.92,
            edgecolor=GRID,
            ncol=2,
            borderpad=0.5,
            handletextpad=0.3,
            columnspacing=1.0,
        )
        # bin colour key (top-right inside map)
        keys = [
            (BIN_EMPTY, "empty bin"),
            (BIN_FULL, "full pallet"),
            (BIN_PART, "part pallet"),
            (BIN_RESV, "allocated to an order"),
            (FLASH[1], "hand pick (now)"),
            (FLASH[3], "forklift in/out (now)"),
        ]
        for k, (c, lab) in enumerate(keys):
            y = 28.2 - k * 1.9
            ax.add_patch(
                Rectangle(
                    (58.4, y - 0.55),
                    1.4,
                    1.1,
                    fc=np.array(c) / 255,
                    ec=INK2,
                    lw=0.4,
                    zorder=3,
                )
            )
            ax.text(60.4, y, lab, fontsize=6.3, va="center", color=INK2, zorder=3)

    # ---- hud ----
    def build_hud(self):
        f = self.fig
        # left strip: title, clock, counters, event feed
        self.hud = f.add_axes([0.004, 0.0, 0.30, 0.31], facecolor=SURFACE)
        self.hud.set_xlim(0, 1)
        self.hud.set_ylim(0, 1)
        self.hud.axis("off")
        h = self.hud
        h.text(
            0.02,
            0.94,
            "CKB WSM SUB 2 - warehouse simulation",
            fontsize=11,
            fontweight="bold",
            color=INK,
            va="center",
        )
        self.clock = h.text(
            0.97,
            0.925,
            "",
            fontsize=19,
            fontweight="bold",
            color=INK,
            ha="right",
            va="center",
        )
        self.sub = h.text(0.02, 0.865, "", fontsize=7.5, color=MUTED, va="center")
        self.counters = h.text(
            0.02, 0.80, "", fontsize=7.5, color=INK, va="top", family="monospace"
        )
        h.text(
            0.02,
            0.49,
            "Latest events",
            fontsize=8.5,
            fontweight="bold",
            color=INK,
            va="center",
        )
        self.feed = h.text(
            0.02,
            0.47,
            "",
            fontsize=6.3,
            color=INK2,
            va="top",
            family="monospace",
            linespacing=1.3,
        )
        # middle strip: truck table
        midi = self.midi = f.add_axes([0.315, 0.0, 0.275, 0.31], facecolor=SURFACE)
        midi.set_xlim(0, 1)
        midi.set_ylim(0, 1)
        midi.axis("off")
        midi.text(
            0.02,
            0.94,
            "Trucks (plan vs reality)",
            fontsize=8.5,
            fontweight="bold",
            color=INK,
            va="center",
        )
        midi.text(
            0.02,
            0.875,
            "truck  type  plan   actual  vs plan   status",
            fontsize=6.8,
            color=MUTED,
            va="center",
            family="monospace",
        )
        self.trow = {}
        ids = sorted(int(t) for t in self.log["trucks"])
        for k, tid in enumerate(ids):
            y = 0.835 - k * 0.062
            dot = Line2D(
                [0.02],
                [y],
                marker="s",
                ms=5.5,
                color=IN_COLOR
                if self.log["trucks"][str(tid)]["kind"] == "IN"
                else OUT_COLOR,
                ls="",
                transform=midi.transAxes,
            )
            midi.add_line(dot)
            t = midi.text(
                0.04, y, "", fontsize=7, color=INK, va="center", family="monospace"
            )
            self.trow[tid] = t
        # right strip: queue chart + utilisation bars
        self.axq = f.add_axes([0.605, 0.115, 0.195, 0.16], facecolor=SURFACE)
        self.axq.set_title(
            "Waiting in line (requests / trucks)",
            fontsize=7.5,
            loc="left",
            color=INK,
            pad=3,
        )
        self.qkeys = [
            ("q_forklift", "forklift", ROLE_COLOR["forklift"]),
            ("q_stacker", "stacker", ROLE_COLOR["stacker"]),
            ("q_picker", "picker", ROLE_COLOR["picker"]),
            ("q_checker", "checker", ROLE_COLOR["checker"]),
            ("q_loader", "loader", ROLE_COLOR["loader"]),
            ("gate", "trucks at gate", INK),
        ]
        T = np.array([s["t"] for s in self.series])
        self.T = T
        ymax = 1
        self.qlines = []
        for key, lab, col in self.qkeys:
            y = np.array([s[key] for s in self.series])
            ymax = max(ymax, y.max())
            (ln,) = self.axq.plot(
                [],
                [],
                color=col,
                lw=1.4,
                label=lab,
                ls="-" if key != "gate" else (0, (3, 2)),
            )
            self.qlines.append((key, ln, y))
        self.axq.set_xlim(0, self.meta["end"])
        self.axq.set_ylim(0, ymax * 1.15 + 0.5)
        ticks = np.arange(0, self.meta["end"], 3600)
        self.axq.set_xticks(ticks)
        self.axq.set_xticklabels(
            [hhmm(self.t0, t) for t in ticks], fontsize=6, color=MUTED
        )
        self.axq.tick_params(axis="y", labelsize=6, colors=MUTED, length=0)
        self.axq.tick_params(axis="x", length=0)
        self.axq.grid(axis="y", color=GRID, lw=0.6)
        for s in self.axq.spines.values():
            s.set_visible(False)
        self.qcursor = self.axq.axvline(0, color=INK2, lw=0.8)
        self.axq.legend(
            fontsize=5.5,
            ncol=2,
            loc="upper left",
            frameon=False,
            handlelength=1.4,
            columnspacing=0.9,
        )
        # utilisation bars
        self.axu = f.add_axes([0.825, 0.115, 0.17, 0.16], facecolor=SURFACE)
        self.axu.set_title(
            "Utilisation so far", fontsize=7.5, loc="left", color=INK, pad=3, x=-0.13
        )
        self.urole = ROLE_ORDER
        self.ubar = self.axu.barh(
            range(len(self.urole)),
            [0] * len(self.urole),
            color=[ROLE_COLOR[r] for r in self.urole],
            height=0.62,
        )
        self.axu.set_yticks(range(len(self.urole)))
        self.axu.set_yticklabels(
            [f"{r} ({self.cfg[ROLE_N[r]]})" for r in self.urole],
            fontsize=6.2,
            color=INK2,
        )
        self.axu.invert_yaxis()
        self.axu.set_xlim(0, 100)
        self.axu.set_xticks([])
        for s in self.axu.spines.values():
            s.set_visible(False)
        self.axu.tick_params(length=0)
        self.utxt = [
            self.axu.text(1, i, "", fontsize=6.2, va="center", color=INK)
            for i in range(len(self.urole))
        ]

    # ---- state ----
    def reset(self, t):
        self.frac = list(self.log["initial_state"])
        self.resv = [0] * len(self.bins)
        self.flash_until = {}
        self.flash_col = {}
        self.ptr = 0
        self.lane_state = {}
        self.lptr = 0
        self.recv_state = {}
        self.rptr = 0
        ev = self.log["bin_ev"]
        while self.ptr < len(ev) and ev[self.ptr][0] <= t:
            _, i, fr, rs, fl = ev[self.ptr]
            self.frac[i], self.resv[i] = fr, rs
            self.ptr += 1
        for i in range(len(self.bins)):
            self.paint(i)
        self.advance_aux(t)
        self.now = t

    def color(self, i, t):
        fu = self.flash_until.get(i)
        if fu is not None and t < fu:
            return FLASH[self.flash_col[i]]
        fr = self.frac[i]
        if fr <= 0:
            return BIN_EMPTY
        if self.resv[i]:
            return BIN_RESV
        return BIN_FULL if fr >= 0.99 else BIN_PART

    def paint(self, i, t=-1e9):
        r0, r1, c0, c1 = self.slice[i]
        self.img[r0:r1, c0:c1] = self.color(i, t)

    def advance_aux(self, t):
        le, re_ = self.log["lane_ev"], self.log["recv_ev"]
        while self.lptr < len(le) and le[self.lptr][0] <= t:
            _, lane, cnt, truck = le[self.lptr]
            if cnt == -1:
                self.lane_state.pop(lane, None)
            else:
                self.lane_state[lane] = (cnt, truck)
            self.lptr += 1
        while self.rptr < len(re_) and re_[self.rptr][0] <= t:
            _, spot, st = re_[self.rptr]
            self.recv_state[spot] = st
            self.rptr += 1

    def update_bins(self, t):
        ev = self.log["bin_ev"]
        changed = set()
        while self.ptr < len(ev) and ev[self.ptr][0] <= t:
            te, i, fr, rs, fl = ev[self.ptr]
            self.frac[i], self.resv[i] = fr, rs
            if fl in FLASH:
                self.flash_until[i] = te + FLASH_S
                self.flash_col[i] = fl
            changed.add(i)
            self.ptr += 1
        expired = [i for i, fu in self.flash_until.items() if fu <= t]
        for i in expired:
            del self.flash_until[i]
            changed.add(i)
        changed.update(self.flash_until.keys())
        for i in changed:
            self.paint(i, t)
        self.im.set_data(self.img)

    # ---- one frame ----
    def render(self, t, speed=None):
        if t < self.now:
            self.reset(t)
        self.now = t
        self.update_bins(t)
        self.advance_aux(t)
        m = self.meta
        # lanes / recv
        for i, (r, tx) in enumerate(zip(self.lane_rect, self.lane_txt)):
            st = self.lane_state.get(i)
            if st:
                kind = self.log["trucks"][str(st[1])]["kind"]
                r.set_facecolor(OUT_COLOR)
                tx.set_text(f"{st[0]}")
            else:
                r.set_facecolor("#ffffff")
                tx.set_text("")
        for i, r in enumerate(self.recv_rect):
            r.set_facecolor("#a9784a" if self.recv_state.get(i) else "#ffffff")
        # agents
        for name, ln in self.agent_art.items():
            x, z, dx, dz, label, carry, active = self.tracks[name].at(t)
            ln.set_data([x], [z])
            ln.set_alpha(1.0 if active else 0.38)
            ln.set_markeredgecolor("black" if active else "white")
            ln.set_markeredgewidth(1.0 if active else 0.4)
            cr = self.carry_art[name]
            if carry:
                cr.set_data([x + 0.9], [z + 0.9])
            else:
                cr.set_data([], [])
        # trucks
        rows_state = {}
        for tid in sorted(self.truck_patch):
            tr = self.log["trucks"][str(tid)]
            name = f"T{tid}"
            tk = self.tracks[name]
            p, tx = self.truck_patch[tid], self.truck_txt[tid]
            states = [s for s in tr["states"] if s[0] <= t]
            st = (
                states[-1][1]
                if states
                else ("en route" if t < tr["arrival"] else "arriving")
            )
            rows_state[tid] = st
            segs = self.log["agents"][name]["segs"]
            vis = (
                bool(segs)
                and segs[0][0] <= t
                and st != "gone"
                and not (segs and t > segs[-1][1] and st == "gone")
            )
            if vis:
                x, z, dx, dz, *_ = tk.at(t)
                if abs(dx) > abs(dz):
                    p.set_width(8.5)
                    p.set_height(3.2)
                    p.set_xy((x - 4.25, z - 1.6))
                else:
                    p.set_width(3.2)
                    p.set_height(8.5)
                    p.set_xy((x - 1.6, z - 4.25))
                late = round((tr["arrival"] - tr["sched"]) / 60)
                tx.set_position((x, z))
                tx.set_rotation(0)
                tx.set_text(f"T{tid} {tr['kind']} {late:+d}m")
                p.set_visible(True)
                tx.set_visible(True)
            else:
                p.set_visible(False)
                tx.set_visible(False)
        # hud
        sx = min(len(self.series) - 1, int(t // 60))
        s = self.series[sx]
        self.clock.set_text(hhmm(self.t0, t))
        self.sub.set_text(
            f"simulated time   |   speed x{speed:g}   |   day start {hhmm(self.t0, 0)}"
            if speed
            else f"simulated time   |   day start {hhmm(self.t0, 0)}"
        )
        self.counters.set_text(
            f"pallets received {s['recv']:>3}   stored {s['stored']:>3}   shipped {s['shipped']:>3}\n"
            f"cartons picked {s['picked']:>5}   replen moves {s['replen']:>3}\n"
            f"gate queue {s['gate']}   gates busy {s['doors']}/{self.cfg['dock_doors']} of {len(m['doors'])}\n"
            f"receiving {s['recv_used']}/{m['recv_spots']}   staging {s['lanes_used']}/{m['lanes']}"
        )
        label = {
            "en route": "not here yet",
            "arriving": "arriving",
            "gate": "waiting at gate",
            "docked": "at dock",
            "leaving": "leaving",
            "gone": "done",
        }
        for tid, tx in self.trow.items():
            tr = self.log["trucks"][str(tid)]
            name = f"T{tid}"
            late = round((tr["arrival"] - tr["sched"]) / 60)
            arrived = t >= tr["arrival"]
            act = rows_state[tid] if arrived else "en route"
            if act == "en route" or act == "arriving":
                act = "en route" if not arrived else "gate"
            actual = hhmm(self.t0, tr["arrival"]) if arrived else "--:--"
            vs = f"{late:+4d} min" if arrived else "        "
            tx.set_text(
                f"T{tid:<3}  {tr['kind']:<3}  {hhmm(self.t0, tr['sched'])}  {actual}  {vs}   {label.get(act, act)}"
            )
            tx.set_fontweight(
                "bold" if act in ("gate", "docked", "leaving") else "normal"
            )
            tx.set_color(INK if act != "gone" else MUTED)
        notes = [n for n in self.log["notes"] if n[0] <= t][-8:]
        self.feed.set_text(
            "\n".join(n[1] if len(n[1]) < 46 else n[1][:45] + "." for n in notes)
        )
        # queue chart
        k = sx + 1
        for key, ln, y in self.qlines:
            ln.set_data(self.T[:k], y[:k])
        self.qcursor.set_xdata([t, t])
        # utilisation
        el = max(t, 60.0)
        for i, role in enumerate(self.urole):
            n = self.cfg[ROLE_N[role]]
            u = 100 * s["busy"][role] / (n * el)
            u = min(100.0, u)
            self.ubar[i].set_width(u)
            self.utxt[i].set_x(u + 1.2)
            self.utxt[i].set_text(f"{u:.0f}%")
        self.fig.canvas.draw()


# ----------------------------------------------------------------------------- outputs
def make_writer(fps):
    plt.rcParams["animation.ffmpeg_path"] = "/usr/bin/ffmpeg"
    return animation.FFMpegWriter(
        fps=fps,
        codec="libx264",
        extra_args=["-pix_fmt", "yuv420p", "-crf", "23", "-preset", "veryfast"],
    )


def write_video(r, path, start, end, speed, fps):
    w = make_writer(fps)
    step = speed / fps
    n = int((end - start) / step) + 1
    with w.saving(r.fig, str(path), dpi=r.fig.dpi):
        r.reset(start)
        for k in range(n):
            r.render(start + k * step, speed)
            w.grab_frame()
            if k % 200 == 0:
                print(f"  frame {k}/{n}", flush=True)
    print("wrote", path)


def dashboard(log, kpis, path):
    plt.rcParams.update(
        {
            "font.size": 9,
            "axes.edgecolor": GRID,
            "axes.labelcolor": INK2,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
        }
    )
    t0 = log["meta"]["t0"]
    cfg = log["meta"]["config"]
    series = log["series"]
    T = np.array([s["t"] for s in series])
    fig, axs = plt.subplots(2, 3, figsize=(18, 10), dpi=110, facecolor=SURFACE)
    fig.suptitle(
        "CKB WSM SUB 2 - simulated day: KPI dashboard",
        x=0.01,
        ha="left",
        fontsize=15,
        fontweight="bold",
        color=INK,
    )

    def clean(ax, title):
        ax.set_facecolor(SURFACE)
        ax.set_title(
            title, loc="left", fontsize=10.5, fontweight="bold", color=INK, pad=8
        )
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
        ax.tick_params(length=0)
        ax.grid(axis="x", color=GRID, lw=0.6)
        ax.set_axisbelow(True)

    def clock_s(txt):
        return parse_clock(txt, t0)

    hours = lambda ax: (
        ax.set_xticks(np.arange(0, kpis["duration_h"] * 3600 + 1, 3600)),
        ax.set_xticklabels(
            [hhmm(t0, t) for t in np.arange(0, kpis["duration_h"] * 3600 + 1, 3600)],
            fontsize=8,
        ),
    )
    # 1 truck timeline
    ax = axs[0, 0]
    clean(
        ax, "Trucks: planned slot (hollow) -> actual arrival (dot), time at dock (bar)"
    )
    rows = kpis["trucks"]
    for r in rows:
        r["t_plan"] = clock_s(r["planned"])
        r["t_arr"] = clock_s(r["arrived"])
        r["t_done"] = r["t_arr"] + r.get("turnaround_min", 0) * 60
        r["t_dock"] = r["t_done"] - r.get("at_dock_min", 0) * 60
    for r in rows:
        y = r["truck"]
        col = IN_COLOR if r["type"] == "IN" else OUT_COLOR
        ax.plot([r["t_plan"], r["t_arr"]], [y, y], color=MUTED, lw=1, zorder=1)
        ax.scatter(
            [r["t_plan"]], [y], s=46, facecolor=SURFACE, edgecolor=col, lw=1.6, zorder=3
        )
        ax.scatter(
            [r["t_arr"]], [y], s=46, color=col, zorder=3, edgecolor=SURFACE, lw=1
        )
        ax.barh(
            y,
            r["t_done"] - r["t_dock"],
            left=r["t_dock"],
            height=0.5,
            color=col,
            alpha=0.85,
            zorder=2,
        )
        ax.text(
            max(r["t_plan"], r["t_arr"], r["t_done"]) + 600,
            y,
            f"{r['late_min']:+d} min",
            va="center",
            fontsize=8,
            color=INK2,
        )
    ax.set_yticks([r["truck"] for r in rows])
    ax.set_yticklabels([f"T{r['truck']} {r['type']}" for r in rows], fontsize=8.5)
    ax.invert_yaxis()
    hours(ax)
    ax.legend(
        handles=[
            Line2D([], [], marker="s", color=IN_COLOR, ls="", label="inbound"),
            Line2D([], [], marker="s", color=OUT_COLOR, ls="", label="outbound"),
        ],
        frameon=False,
        fontsize=8,
        loc="upper right",
        bbox_to_anchor=(1.0, 1.0),
    )
    # 2 queues
    ax = axs[0, 1]
    clean(ax, "Waiting in line over the day")
    ax.grid(axis="y", color=GRID, lw=0.6)
    ax.grid(axis="x", visible=False)
    for key, lab, col in [
        ("q_forklift", "forklift", ROLE_COLOR["forklift"]),
        ("q_stacker", "stacker", ROLE_COLOR["stacker"]),
        ("q_picker", "picker", ROLE_COLOR["picker"]),
        ("q_checker", "checker", ROLE_COLOR["checker"]),
        ("q_loader", "loader", ROLE_COLOR["loader"]),
        ("gate", "trucks at gate", INK),
    ]:
        y = np.array([s[key] for s in series])
        ax.plot(
            T, y, color=col, lw=1.6, label=lab, ls="-" if key != "gate" else (0, (3, 2))
        )
    ax.legend(frameon=False, fontsize=8, ncol=3, loc="upper right")
    hours(ax)
    ax.set_ylabel("requests waiting")
    # 3 utilisation + waits
    ax = axs[0, 2]
    clean(ax, "Utilisation (bar) and average wait for a free resource")
    roles = ROLE_ORDER
    util = [kpis["utilisation_pct"][r] for r in roles]
    ax.barh(range(len(roles)), util, color=[ROLE_COLOR[r] for r in roles], height=0.6)
    for i, r in enumerate(roles):
        ax.text(
            util[i] + 0.8,
            i,
            f"{util[i]:.0f}%   wait avg {kpis['avg_wait_min'][r]:.1f} / p90 {kpis['p90_wait_min'][r]:.1f} min",
            va="center",
            fontsize=8.5,
            color=INK2,
        )
    ax.set_yticks(range(len(roles)))
    ax.set_yticklabels([f"{r} ({cfg[ROLE_N[r]]})" for r in roles], fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, max(60, max(util) * 2.6))
    ax.set_xlabel("% of the simulated day busy")
    # 4 throughput
    ax = axs[1, 0]
    clean(ax, "Cumulative pallets")
    ax.grid(axis="y", color=GRID, lw=0.6)
    ax.grid(axis="x", visible=False)
    for key, lab, col, ls in [
        ("recv", "received (unloaded)", IN_COLOR, "-"),
        ("stored", "put away in rack", IN_COLOR, (0, (4, 2))),
        ("shipped", "shipped (loaded)", OUT_COLOR, "-"),
    ]:
        ax.plot(T, [s[key] for s in series], color=col, lw=1.8, ls=ls, label=lab)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    hours(ax)
    # 5 staging dwell
    ax = axs[1, 1]
    clean(ax, "Outbound goods waiting in staging for the truck (min)")
    outs = [r for r in rows if r["type"] == "OUT"]
    ax.barh(
        range(len(outs)),
        [r["staged_wait_min"] for r in outs],
        color=OUT_COLOR,
        height=0.6,
    )
    for i, r in enumerate(outs):
        ax.text(
            r["staged_wait_min"] + 3,
            i,
            f"{r['staged_wait_min']:.0f}  (ready {r['goods_ready']}, truck {r['arrived']})",
            va="center",
            fontsize=8,
            color=INK2,
        )
    ax.set_yticks(range(len(outs)))
    ax.set_yticklabels([f"T{r['truck']}" for r in outs])
    ax.invert_yaxis()
    ax.set_xlim(0, max([r["staged_wait_min"] for r in outs] + [30]) * 1.7)
    # 6 lateness
    ax = axs[1, 2]
    clean(
        ax,
        "Truck lateness vs plan (min)  |  arrival order: "
        + "-".join(str(x) for x in kpis["arrival_sequence"][:12]),
    )
    late = [r["late_min"] for r in rows]
    ax.barh(
        range(len(rows)),
        late,
        color=[IN_COLOR if r["type"] == "IN" else OUT_COLOR for r in rows],
        height=0.6,
    )
    ax.axvline(0, color=INK2, lw=1)
    for i, v in enumerate(late):
        ax.text(
            v + (4 if v >= 0 else -4),
            i,
            f"{v:+d}",
            va="center",
            ha="left" if v >= 0 else "right",
            fontsize=8,
            color=INK2,
        )
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([f"T{r['truck']}" for r in rows])
    ax.invert_yaxis()
    ax.set_xlabel(
        "early (left)  /  late (right)      blue = inbound truck, orange = outbound truck"
    )
    ax.set_xlim(min(late) - 25, max(late) + 30)
    fig.text(
        0.01,
        0.005,
        f"Stock check: start {kpis['stock_check']['initial']:,} + inbound {kpis['stock_check']['inbound']:,} "
        f"- outbound {kpis['stock_check']['outbound']:,} = end {kpis['stock_check']['final']:,}  "
        f"({'balanced' if kpis['stock_check']['balanced'] else 'MISMATCH'}).   Times are model assumptions, not measurements.",
        fontsize=8.5,
        color=MUTED,
    )
    fig.tight_layout(rect=(0, 0.02, 1, 0.965))
    fig.savefig(path, facecolor=SURFACE)
    print("wrote", path)


def busiest_window(log, minutes):
    """Start (s) of the window with the most started movements."""
    starts = []
    for a in log["agents"].values():
        starts += [s[0] for s in a["segs"]]
    starts = np.array(sorted(starts))
    best, best_t = -1, 0
    w = minutes * 60
    for t in range(0, int(log["meta"]["end"] - w), 300):
        c = np.searchsorted(starts, t + w) - np.searchsorted(starts, t)
        if c > best:
            best, best_t = c, t
    return best_t


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--log", default="out/sim_log.json")
    ap.add_argument("--kpis", default="out/kpis.json")
    ap.add_argument(
        "--mode",
        choices=["timelapse", "clip", "still", "dashboard", "live"],
        default="timelapse",
    )
    ap.add_argument("--out", default=None)
    ap.add_argument(
        "--speed", type=float, default=None, help="simulated seconds per video second"
    )
    ap.add_argument("--fps", type=int, default=12)
    ap.add_argument(
        "--start", default=None, help="HH:MM (clip) - default: busiest window"
    )
    ap.add_argument("--duration", type=float, default=30, help="minutes (clip)")
    ap.add_argument("--at", default="11:40", help="HH:MM (still)")
    a = ap.parse_args()
    log = json.load(open(a.log))
    outdir = Path(a.log).parent
    t0, end = log["meta"]["t0"], log["meta"]["end"]
    if a.mode == "dashboard":
        dashboard(log, json.load(open(a.kpis)), a.out or outdir / "kpi_dashboard.png")
        return
    r = Renderer(log)
    if a.mode == "still":
        t = parse_clock(a.at, t0)
        r.reset(t)
        r.render(t, None)
        r.fig.savefig(a.out or outdir / "frame.png", facecolor=SURFACE)
        print("wrote", a.out or outdir / "frame.png")
    elif a.mode == "timelapse":
        write_video(
            r, a.out or outdir / "day_timelapse.mp4", 0, end, a.speed or 240, a.fps
        )
    elif a.mode == "clip":
        start = parse_clock(a.start, t0) if a.start else busiest_window(log, a.duration)
        print("clip starts at", hhmm(t0, start))
        write_video(
            r,
            a.out or outdir / "detail_clip.mp4",
            start,
            start + a.duration * 60,
            a.speed or 15,
            a.fps if a.fps != 12 else 15,
        )
    else:
        matplotlib.use("TkAgg", force=True)
        speed = a.speed or 60
        state = {"t": 0.0}

        def upd(_):
            state["t"] = min(end, state["t"] + speed / 20)
            r.render(state["t"], speed)
            return []

        animation.FuncAnimation(r.fig, upd, interval=50, cache_frame_data=False)
        plt.show()


if __name__ == "__main__":
    main()
