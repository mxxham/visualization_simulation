# CKB WSM SUB 2 — warehouse simulation

Discrete-event simulation (SimPy) of the two-shift day at CKB WSM SUB 2
(Surabaya): real rack layout, real opening stock, real outbound shipments,
late/scrambled truck arrivals, and the real staffing/vehicle model of the
warehouse.

## Run

```bash
python wms_sim.py --out out            # real schedule data/schedule.json
python wms_sim.py --seed 13            # other arrival randomness
python wms_sim.py --no-schedule        # fully synthetic day
python viewer.py ...                   # see VIEWER.md / viewer.html
```

Useful flags: `--seed, --trucks-in, --trucks-out, --docks, --arrival-order,
--schedule, --overrides '{"key": value, ...}'` (any Config key, e.g. to change
an assumption below without editing code).

## Operating rules modelled

- **Hours 08:00–21:00, two shifts.** Shift 1 08:00–17:00 with break
  11:45–13:00, shift 2 13:00–21:00 with break 17:00–18:00 (all in Config:
  `shift1, shift2, break1, break2`). Staff on an unfinished break do not start
  new tasks; a task already in progress finishes first (assumption e below).
- **2 operators (`op1`, `op2`) + 2 forklifts (`F1,F2`) + 2 reach trucks
  (`R1,R2`).** A task needs ONE operator plus ONE free vehicle of the right
  type (`op_veh` holds both for the whole task), so an operator can never hold
  two vehicles. Vehicles may sit idle while operators are the limit — both
  utilisations are reported.
- **Vehicle rules:** reach trucks on rack levels B–E (put-away, upper-level
  picks, lowering a pallet); forklifts on level A/floor; truck load and unload
  need an operator + a vehicle of either type (forklift preferred).
- **Trucks are not on time** and arrive in a different order than planned
  (`--arrival-order`); real shipments come from `data/schedule.json`.
- **Pickface rule (docs/ALLOCATOR.md):** any quantity < UPP — partial
  put-away, leftovers of opened pallets, bin-to-bin remainders — goes into the
  SKU's pickface when free, else the nearest empty level-A bin (overflow), else
  it stays put and is retried every `t_reassign_retry`. Full (== UPP) pallets
  go pickface-if-free, else `BCDE` reserve (upstream behaviour).
- **Checkers (`ch1,ch2`) sit at the gate:** every truck — inbound and outbound —
  waits at the gate until a checker is free for the document check (inbound
  trucks additionally check each pallet: `t_check_pallet`). Checkers do not
  drive vehicles and do not do put-away.
- **Loaders (`ld1..ld4`) load outbound trucks** (`loaders_per_truck` of them at
  a time), helped by an operator+vehicle for the truck-side work. Loading one
  truck takes `t_load_min_min..t_load_max_min` = **15–20 min**, growing with
  its quantity and the truck type's `truck_capacity`. Inbound unloading is an
  operator+vehicle task; one loader may assist (halves per-pallet time), see
  assumption (b).

## Assumptions to confirm

These are guesses modelled as Config values (change them via `--overrides`,
no code edit needed). The user will confirm each; nothing below is hidden in
code:

| # | Assumption | Config key(s) | Default (guess) |
|---|------------|---------------|-----------------|
| a1 | Op1 works shift 1 (08–17), op2 shift 2 (13–21); 13–17 overlap has both | `staff_duty["operator"]` | `[1, 2]` |
| a2 | Checkers ch1 shift1 / ch2 shift2 | `staff_duty["checker"]` | `[1, 2]` |
| a3 | Loaders 2 per shift | `staff_duty["loader"]` | `[1,1,2,2]` |
| c  | Pickers pk1,pk2 work all day 08–21 and take both breaks | `staff_duty["picker"]` | `[3, 3]` |
| b  | The loaders unload inbound trucks (loader assist halves per-pallet unload while forklifts do the heavy pallets) | `loaders_unload_inbound` | `True` |
| e  | A task started at 11:40 that ends after 11:45 is just finished; the break absorbs the remainder | behaviour (flag set at window start) | yes |
| d  | Truck capacities by type (pallets): CDD 6, CDE 10, WBX 16, C20 8, GRM 10, TRAGA 14 | `truck_capacity` | see Config |
| —  | Reach-truck cut at level B (`stacker_min_lv=1`: B–E reach, A/floor forklift) | `stacker_min_lv` | `1` |
| —  | `loaders_per_truck = 2` loaders per outbound load | `loaders_per_truck` | `2` |
| —  | An operator+vehicle is required for every truck load (not loaders-only) | `load_operator_vehicle` | `True` |
| —  | Loading base 15 min + up to 5 min by the load/capacity ratio, clamped 15–20 | `t_load_base_min, t_load_min_min, t_load_max_min` | 15/15/20 |
| —  | The day can run past 21:00 when late trucks are still loading (no hard shutdown; duty ends but in-progress work continues) | behaviour | yes |
| —  | Checker gate check per truck = `t_doc_check` 3 min (+ per-pallet 70 s inbound) | `t_doc_check, t_check_pallet` | unchanged |

**Unresolved numbers (explicitly still guesses):**
`truck_capacity` values (d), all shift assignments (a), the loader-assisted
inbound-unload fact (b), and the loader count per truck.

## KPIs (`out/kpis.json`)

- `operator_util_pct`, `vehicle_util_pct` (`forklift` / `reach`)
- `avg_wait_op_min`, `p90_wait_op_min` — wait for an operator
- `break_wait_tasks` — task acquisitions that had to wait for a break to end
- `trucks_by_type` — counts per real truck type (schedule `truck_type`)
- per-truck rows: `load_min` (actual whole-truck loading time), `loaders_used`,
  plus the older gate/dock/turnaround/wait rows
- `stock_check.balanced` — initial + inbound − outbound == final cartons

## Run log additions (`out/sim_log.json`)

- `"breaks"` — `[[agent, role, t0, t1], ...]` every break actually taken
- `"vehicle_segs"` — `{F1/F2/R1/R2: [[t0, t1, operator, label], ...]}` every
  operator-held vehicle interval (feeds the viewer's vehicle bars and the
  no-double-hold test)
- per-agent `task_starts` / `duty` / `breaks` in `"agents"` (viewer shows the
  staff mini-timeline with hatched break bands and "(on break)" badges)
- `series` entries gain `busy_op`, `busy_f`, `busy_r`

## Tests

```bash
python tests/test_fefo.py   # layout + FEFO — untouched, must stay green
python tests/test_ops.py    # breaks / one-vehicle-per-operator / pickface /
                            # 15–20 min loading / stock balance
```

## Data

`data/{bins,items,stock}.csv` extract the WMS repo seed (24 Sep 2026 stock);
`data/schedule.json` the day's outbound shipments (extracted by
`extract_schedule.py`). Layout, FEFO allocation and the pickface mechanics are
kept exactly as in the WMS repo.
