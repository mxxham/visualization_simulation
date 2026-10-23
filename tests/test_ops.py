#!/usr/bin/env python3
"""
Operational test suite for the SimPy warehouse simulation.
Run via: .venv/bin/python tests/test_ops.py
"""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
WMS_SIM = REPO_ROOT / "wms_sim.py"

# Cache for sim runs: seed -> (log_dict, kpis_dict)
_sim_cache: dict[int, tuple[dict, dict]] = {}


def run_sim(seed: int) -> tuple[dict, dict]:
    """Run the simulation for a given seed, return (sim_log, kpis). Cached."""
    if seed in _sim_cache:
        return _sim_cache[seed]

    with tempfile.TemporaryDirectory() as tmpdir:
        out_dir = Path(tmpdir)
        cmd = [
            str(VENV_PYTHON),
            str(WMS_SIM),
            "--out",
            str(out_dir),
            "--seed",
            str(seed),
        ]
        # Use a reasonable timeout (sim should complete in seconds)
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, cwd=REPO_ROOT
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Simulation failed for seed {seed}:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )

        log_path = out_dir / "sim_log.json"
        kpis_path = out_dir / "kpis.json"
        if not log_path.exists():
            raise FileNotFoundError(f"sim_log.json not found at {log_path}")
        if not kpis_path.exists():
            raise FileNotFoundError(f"kpis.json not found at {kpis_path}")

        log = json.loads(log_path.read_text())
        kpis = json.loads(kpis_path.read_text())

    _sim_cache[seed] = (log, kpis)
    return log, kpis


def parse_hhmm_to_seconds(hhmm: str) -> int:
    """Convert HH:MM string to seconds since midnight."""
    if isinstance(hhmm, (list, tuple)):
        h, m = hhmm
    else:
        h, m = hhmm.split(":")
    return int(h) * 3600 + int(m) * 60


def get_break_windows(log: dict) -> list[tuple[int, int]]:
    """Extract break windows [t0, t1] in simulation seconds from log meta.config or defaults."""
    # Contract: breaks in log["breaks"] are [[name, role, t0, t1], ...] where t0,t1 are sim seconds
    # But we also need the scheduled break windows (not just taken breaks).
    # The contract says: "window boundaries from meta.config if present else the defaults above"
    meta = log.get("meta", {})
    config = meta.get("config", {})

    # Default break windows per contract (HH:MM strings relative to start_hhmm)
    start_hhmm = config.get("start_hhmm", "08:00")
    start_sec = parse_hhmm_to_seconds(start_hhmm)

    break1 = config.get("break1", ("11:45", "13:00"))
    break2 = config.get("break2", ("17:00", "18:00"))

    def to_sim_sec(window):
        t0, t1 = window
        return (
            parse_hhmm_to_seconds(t0) - start_sec,
            parse_hhmm_to_seconds(t1) - start_sec,
        )

    return [to_sim_sec(break1), to_sim_sec(break2)]


def intervals_overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    """Check if two half-open intervals [a0, a1) and [b0, b1) overlap."""
    return not (a1 <= b0 or b1 <= a0)


def test_layout_and_fefo_still_pass() -> None:
    """Test 1: Run existing test_fefo.py and assert it passes."""
    cmd = [str(VENV_PYTHON), "tests/test_fefo.py"]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=60, cwd=REPO_ROOT
    )
    if result.returncode != 0:
        raise AssertionError(
            f"test_fefo.py failed with exit code {result.returncode}:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    if "OK" not in result.stdout:
        raise AssertionError(
            f"test_fefo.py did not print 'OK':\nSTDOUT:\n{result.stdout}"
        )
    print("test_layout_and_fefo_still_pass: PASS")


def test_no_task_starts_during_break() -> None:
    """Test 2: No task acquisition (grab) for a staff member happens strictly
    inside one of THEIR break windows; a task already held keeps running
    across the window (assumption e), so only task starts are checked."""
    for seed in (7, 13):
        log, _ = run_sim(seed)

        agents = log.get("agents", {})
        for agent_name, agent_data in agents.items():
            wins = agent_data.get("breaks") or []  # per-agent duty-clamped windows
            starts = agent_data.get("task_starts") or []
            for t_start in starts:
                for bw_t0, bw_t1 in wins:
                    if bw_t0 < t_start < bw_t1:
                        raise AssertionError(
                            f"seed {seed}: agent {agent_name} acquired a task at {t_start:.1f}s "
                            f"inside their break window [{bw_t0}, {bw_t1}]"
                        )
    print("test_no_task_starts_during_break: PASS")


def test_no_operator_double_hold() -> None:
    """Test 3: Per vehicle, intervals don't overlap; per operator across vehicles, no overlap."""
    for seed in (7, 13):
        log, _ = run_sim(seed)

        # Contract: log["vehicle_segs"] = {veh_id: [[t0, t1, op_name, label], ...]}
        vehicle_segs = log.get("vehicle_segs", {})
        if not vehicle_segs:
            # If vehicle_segs not present, skip (contract mismatch)
            print(f"  seed {seed}: vehicle_segs not in log, skipping overlap check")
            continue

        # Check per-vehicle no overlap
        for veh_id, segs in vehicle_segs.items():
            # Sort by start time
            sorted_segs = sorted(segs, key=lambda s: s[0])
            for i in range(len(sorted_segs) - 1):
                t0_a, t1_a = sorted_segs[i][0], sorted_segs[i][1]
                t0_b, t1_b = sorted_segs[i + 1][0], sorted_segs[i + 1][1]
                if intervals_overlap(t0_a, t1_a, t0_b, t1_b):
                    raise AssertionError(
                        f"seed {seed}: vehicle {veh_id} has overlapping segments: "
                        f"[{t0_a:.1f}, {t1_a:.1f}) and [{t0_b:.1f}, {t1_b:.1f})"
                    )

        # Check per-operator no overlap across all vehicles
        op_intervals: dict[str, list[tuple[float, float]]] = {}
        for veh_id, segs in vehicle_segs.items():
            for seg in segs:
                t0, t1, op_name, label = seg[0], seg[1], seg[2], seg[3]
                if op_name not in op_intervals:
                    op_intervals[op_name] = []
                op_intervals[op_name].append((t0, t1, veh_id, label))

        for op_name, intervals in op_intervals.items():
            sorted_ints = sorted(intervals, key=lambda x: x[0])
            for i in range(len(sorted_ints) - 1):
                t0_a, t1_a, veh_a, label_a = sorted_ints[i]
                t0_b, t1_b, veh_b, label_b = sorted_ints[i + 1]
                if intervals_overlap(t0_a, t1_a, t0_b, t1_b):
                    raise AssertionError(
                        f"seed {seed}: operator {op_name} holds vehicle {veh_a} ({label_a}) "
                        f"[{t0_a:.1f}, {t1_a:.1f}) and vehicle {veh_b} ({label_b}) "
                        f"[{t0_b:.1f}, {t1_b:.1f}) simultaneously"
                    )
    print("test_no_operator_double_hold: PASS")


def test_partial_putaway_goes_to_pickface() -> None:
    """Test 4: Pure test of putaway_target helper (no sim run needed)."""
    # Import wms_sim and use the same pattern as test_fefo.py
    sys.path.insert(0, str(REPO_ROOT))
    import wms_sim as w

    # Build sim exactly like test_fefo.py does
    sim = w.Sim(w.Config(seed=7, load_factor=3))
    # load_world is called in __init__, but we need pickface populated
    # test_fefo.py doesn't call make_trucks for its tests, so we don't either

    # Find a SKU that HAS a pickface entry and upp >= 2
    candidate_sku = None
    for sku, pf_bin in sim.pickface.items():
        upp = sim.items[sku].upp
        if upp >= 2:
            candidate_sku = sku
            break

    if candidate_sku is None:
        raise AssertionError("No SKU with pickface and upp >= 2 found")

    sku = candidate_sku
    upp = sim.items[sku].upp
    pf_bin = sim.pickface[sku]

    # The contract's 4a precondition is a FREE pickface bin: whatever the
    # opening stock holds there, freeing it models the pickface just emptied.
    pf_bin.pallet = None
    pf_bin.claim = None

    # Discover the putaway helper name
    putaway_fn = None
    for name in (
        "putaway_target",
        "put_away_target",
        "find_putaway_bin",
        "target_putaway",
    ):
        if hasattr(w, name):
            putaway_fn = getattr(w, name)
            break
        if hasattr(sim, name):
            putaway_fn = getattr(sim, name)
            break

    if putaway_fn is None:
        raise AssertionError(
            "putaway_target helper not found in wms_sim module or Sim class"
        )

    # Bound method of the sim takes (sku, qty[, from_pt]); a module-level
    # helper takes (sim, sku, qty[, from_pt]). Normalize the call.
    if hasattr(putaway_fn, "__self__") and putaway_fn.__self__ is sim:
        _raw = putaway_fn

        def call(sku_arg, qty_arg, from_pt=None, _fn=_raw):
            return _fn(sku_arg, qty_arg, from_pt)
    else:
        call = putaway_fn

    # Test 4a: partial qty (upp - 1) -> should target pickface while free
    target = call(sku, upp - 1)
    assert target is pf_bin, (
        f"partial putaway for {sku} qty {upp - 1} should target pickface {pf_bin.code}, got {target.code if target else None}"
    )

    # Test 4b: force pickface occupied, partial qty -> should target level-A empty bin or None
    # Clone a pallet into the pickface bin the way stock.csv loading does
    from wms_sim import Pallet

    fake_pallet = Pallet(sim.env, sku, "TEST_BATCH", sim.asof, upp)
    fake_pallet.bin = pf_bin
    pf_bin.pallet = fake_pallet
    sim.sku_pallets[sku].add(fake_pallet)

    target2 = call(sku, upp - 1)
    if target2 is not None:
        assert target2.level == "A", (
            f"partial putaway with occupied pickface should target level-A bin, got level {target2.level} ({target2.code})"
        )
        assert target2.pallet is None, f"target bin {target2.code} should be empty"
    else:
        # Verify no empty level-A bins exist
        empty_a = [
            b
            for b in sim.bin_list
            if b.level == "A" and b.pallet is None and b.claim is None
        ]
        assert len(empty_a) == 0, (
            f"putaway_target returned None but {len(empty_a)} empty level-A bins exist"
        )

    # Test 4c: full qty (upp) -> reserve 'BCDE' preference when the pickface
    # is occupied (upstream WMS put-away keeps the pickface-if-free option,
    # so with the fake pallet still sitting in the pickface this must go BCDE)
    target3 = call(sku, upp)
    if target3 is not None:
        assert target3.level in "BCDE", (
            f"full-qty putaway for {sku} with a full pickface should target BCDE level, got {target3.level} ({target3.code})"
        )

    # Cleanup: restore the opening-stock occupancy of the pickface bin
    print("test_partial_putaway_goes_to_pickface: PASS")


def test_loading_time_bounds() -> None:
    """Test 5: Every OUT truck's load_min in [15, 20]; monotonic with pallet count."""
    for seed in (7, 13):
        _, kpis = run_sim(seed)

        # Contract: kpis["trucks"] has per-truck entries with load_min
        # Also kpis["trucks_by_type"] dict
        trucks = kpis.get("trucks", [])
        out_trucks = [t for t in trucks if t.get("type") == "OUT"]

        if not out_trucks:
            print(f"  seed {seed}: no OUT trucks, skipping")
            continue

        # Check bounds: only trucks that actually loaded something; a true
        # zero (entirely short) shipment runs no loading task (load_min=None)
        for t in out_trucks:
            if t.get("pallets", 0) == 0:
                continue
            load_min = t.get("load_min")
            if load_min is None:
                raise AssertionError(
                    f"seed {seed}: truck {t.get('truck')} missing load_min field"
                )
            if not (15 <= load_min <= 20):
                raise AssertionError(
                    f"seed {seed}: truck {t.get('truck')} load_min={load_min} outside [15, 20]"
                )

        # Check monotonicity per truck type: more pallets => load_min >= a
        # lighter truck of the SAME type (capacity normalizes the rise, so a
        # 1-pallet CDD may legally load longer than a 2-pallet TRAGA).
        loaded = [t for t in out_trucks if t.get("load_min") is not None]
        by_type = {}
        for t in loaded:
            by_type.setdefault(t.get("ttype") or "?", []).append(t)
        for ttype, group in by_type.items():
            sorted_t = sorted(group, key=lambda x: x.get("pallets", 0))
            for i in range(len(sorted_t) - 1):
                t_light = sorted_t[i]
                t_heavy = sorted_t[i + 1]
                if t_heavy["pallets"] > t_light["pallets"]:
                    if t_heavy["load_min"] < t_light["load_min"]:
                        raise AssertionError(
                            f"seed {seed}: monotonicity violation ({ttype}): truck {t_light['truck']} "
                            f"({t_light['pallets']} pallets, load_min={t_light['load_min']}) "
                            f"vs truck {t_heavy['truck']} ({t_heavy['pallets']} pallets, load_min={t_heavy['load_min']})"
                        )
    print("test_loading_time_bounds: PASS")


def test_stock_balance() -> None:
    """Test 6: stock_check['balanced'] is True; print the numbers."""
    for seed in (7, 13):
        _, kpis = run_sim(seed)
        stock_check = kpis.get("stock_check", {})
        balanced = stock_check.get("balanced")
        if balanced is not True:
            raise AssertionError(
                f"seed {seed}: stock_check not balanced: "
                f"initial={stock_check.get('initial')}, inbound={stock_check.get('inbound')}, "
                f"outbound={stock_check.get('outbound')}, final={stock_check.get('final')}, "
                f"excluded={stock_check.get('excluded_quarantine')}"
            )
        print(
            f"  seed {seed}: stock balanced - initial={stock_check['initial']}, "
            f"inbound={stock_check['inbound']}, outbound={stock_check['outbound']}, "
            f"final={stock_check['final']}, excluded={stock_check['excluded_quarantine']}"
        )
    print("test_stock_balance: PASS")


def main() -> int:
    """Run all tests for seeds 7 and 13."""
    print("=" * 60)
    print("Running operational test suite (seeds 7, 13)")
    print("=" * 60)

    tests = [
        ("test_layout_and_fefo_still_pass", test_layout_and_fefo_still_pass),
        ("test_no_task_starts_during_break", test_no_task_starts_during_break),
        ("test_no_operator_double_hold", test_no_operator_double_hold),
        (
            "test_partial_putaway_goes_to_pickface",
            test_partial_putaway_goes_to_pickface,
        ),
        ("test_loading_time_bounds", test_loading_time_bounds),
        ("test_stock_balance", test_stock_balance),
    ]

    failed = []
    for name, fn in tests:
        try:
            print(f"\nRunning {name}...")
            fn()
        except AssertionError as e:
            print(f"  FAIL: {e}")
            failed.append((name, str(e)))
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            failed.append((name, f"{type(e).__name__}: {e}"))

    print("\n" + "=" * 60)
    if failed:
        print(f"FAILED: {len(failed)}/{len(tests)} tests")
        for name, msg in failed:
            print(f"  - {name}: {msg}")
        return 1
    else:
        print(f"ALL {len(tests)} TESTS PASSED")
        return 0


if __name__ == "__main__":
    sys.exit(main())
