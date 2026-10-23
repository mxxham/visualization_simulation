"""FEFO is absolute: a later-expiry pallet is never reserved while an earlier-expiry pallet of the same SKU still has free stock.
Also: reservations never exceed stock, and the layout matches the repo's coordinate formulas."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wms_sim as w

def test_fefo(seed):
    sim = w.Sim(w.Config(seed=seed, load_factor=3))
    trucks = sim.make_trucks()
    n_tasks = 0
    for t in trucks:
        if not t.ship:
            continue
        for sku, q in t.ship.lines:
            tasks, short = sim.allocate_line(sku, q)
            n_tasks += len(tasks)
            if tasks:
                latest = max(x.pallet.expiry for x in tasks)
                for p in sim.sku_pallets[sku]:
                    if p.expiry < latest:
                        assert p.qty - p.reserved == 0, f"seed {seed}: {sku} took {latest} while {p.expiry} still free"
            assert sum(x.take for x in tasks) + short == q
            for p in sim.sku_pallets[sku]:
                assert 0 <= p.reserved <= p.qty
    return n_tasks

def test_layout():
    sim = w.Sim(w.Config())
    b = sim.bins
    assert len(b) == 2150                                   # 2,160 rack bins in the seed minus 10 phantom bins
    assert not any(c.startswith("CG") for c in b)           # aisle CG does not exist
    assert "CE33A01" not in b and "CC19A01" not in b and "CA20A01" not in b
    # 0006_back_to_back_racks.sql: x = ((rack-1)%20)*2.7 + (pos-0.5)*2.7/2 ; y = level*1.6 ; z = aisle*5.6 -/+ 0.6
    x = b["CA01A01"]; assert (round(x.x, 3), x.y, round(x.z, 2)) == (0.675, 0.0, -0.6)
    y = b["CA21A01"] if "CA21A01" in b else b["CB21A01"]
    assert round(y.z - b[y.code.replace("21", "01")].z, 2) == 1.2          # rack 21 sits directly behind rack 01
    assert y.x == b[y.code.replace("21", "01")].x
    z = b["CF40E02"]; assert (round(z.x, 3), round(z.y, 1), round(z.z, 2)) == (53.325, 6.4, 28.6)
    assert sim.initial_cartons == 52049                     # 52,078 in the seed minus 29 in QUARANTINE

if __name__ == "__main__":
    test_layout(); print("layout OK")
    total = sum(test_fefo(s) for s in range(1, 21)); print(f"FEFO OK on 20 seeds ({total} allocation tasks)")
