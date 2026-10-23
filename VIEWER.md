# Viewer — animasi hasil simulasi CKB WSM SUB 2

Replay canvas dari `out/<run>/sim_log.json` yang dihasilkan `wms_sim.py`.

## Hasil video & PNG (visualize.py)

Selain viewer browser, `visualize.py` merender hasil simulasi menjadi MP4/PNG
(matplotlib + ffmpeg). Run-nya harus sudah ada di `out/<run>/sim_log.json`.

```bash
cd ~/wms-sim
.venv/bin/python visualize.py --mode timelapse --log out/default/sim_log.json        # whole day -> day_timelapse.mp4
.venv/bin/python visualize.py --mode clip --start 09:00 --duration 45 --log out/default/sim_log.json
.venv/bin/python visualize.py --mode still --at 09:55 --log out/default/sim_log.json # one PNG frame
.venv/bin/python visualize.py --mode dashboard --log out/default/sim_log.json --kpis out/default/kpis.json
```

- `timelapse` default speed x240 (satu hari ≈ 2 menit video); `clip` default x15,
  tanpa `--start` memilih jendela tersibuk otomatis.
- `dashboard` menggambar 6 panel KPI: garis waktu truk (jadwal vs nyata), antrian,
  utilisasi + p90 menunggu per peran, pallet kumulatif, tunggu staging, dan keterlambatan.
- Peta mengikuti lantai operasional yang benar: gerbang di dinding barat (G1–G7,
  `dock_doors` aktif), staging & receiving di lantai barat, blok rak CA–CF memanjang
  ke timur.
- `--mode live` butuh display (jendela interaktif TkAgg).

## Dashboard web (dashboard.html)

`dashboard.html` menampilkan ulang frame dashboard `visualize.py` secara
interaktif di browser — peta warehouse, jam, tabel truk (plan vs nyata),
grafik antrian, bar utilisasi, dan umpan event, memutar/scrub `sim_log.json`
run yang dipilih (kecepatan ×1–×240). Buka `http://localhost:8000/dashboard.html?run=<nama>`
atau lewat tautan **Dashboard KPI** di header `viewer.html`.

Tombol **⚙ Konfigurasi** membuka panel untuk mengubah jumlah staf (picker,
checker, pemuat, forklift, stacker), **jumlah truk hari ini** — truk keluar
(outbound) dan truk masuk (inbound), masing-masing 0–20 — dan durasi tiap
operasi (muat/bongkar, scan, cek palet, dokumen, audit, alokasi, pick, posisi
forklift, slot truk), lalu langsung menjalankan simulasi baru
(`POST /api/simulate` → `wms_sim.py --overrides {...}`); run baru otomatis
dimuat dan muncul di dropdown **Hasil**. Nilai yang dipakai tiap run tersimpan di
`out/<run>/sim_log.json` → `meta.config` dan `meta.truck_plan`. CLI setara:
`.venv/bin/python wms_sim.py --out out/x --overrides '{"t_check_pallet":120,"n_pickers":3,"dock_doors":3}'`.

Truk keluar memakai kiriman jadwal paling awal; bila angkanya melebihi jadwal,
sisanya dibuat sintetis. Truk masuk disebar ±30 menit mulai pagi. Di atas tabel
truk ada ringkasan `truk N = A masuk + B keluar`, `tiba x/N`, dan `selesai y/N`.

## Menjalankan

```bash
cd ~/wms-sim
python3 serve.py          # buka http://localhost:8000/
python3 serve.py -p 9100  # port lain bila 8000 terpakai
```

Tidak perlu dependencies tambahan (stdlib + browser). Bila port masih dipakai
instance lama, hentikan instance lama dulu (`pkill -f serve.py`) atau pilih port lain.

## Panel kanan

- **Pegawai & peralatan** — spinner Picker 2 / Checker 2 / Stacker (reach) 2 / Forklift 2 /
  Pemuat 4 (angka studi banding; tidak ada operator/controller/restocker),
  plus Truk dan Seed, dan centang **Use real orders** untuk memakai pesanan nyata.
  Tombol **▶ Jalankan ulang simulasi** memanggil `wms_sim.py` dengan jumlah tersebut
  (subprocess, ±1 detik), menulis `out/run-<waktu>/`, lalu langsung memuat hasilnya.
- **Pesanan** (kartu Simulasi) — jumlah kiriman riil dari sheet "Schedule of the day"
  bila dipakai, mis. `11 kiriman nyata (schedule.json)`.
- **Kinerja** — utilisasi % per peran, rata-rata/p90 waktu menunggu, badge stok
  seimbang (awal + masuk − keluar = akhir), grafik antrian.
- **Truk** — jadwal vs waktu tiba (kolom **Selisih** merah bila telat > 15 menit),
  **Tujuan** (kota kiriman), durasi putar, jumlah pallet; arahkan kursor untuk nomor
  shipment/pelanggan/jenis truk. Dengan pesanan nyata, **Rencana** = Arrival Slot Time
  dari sheet.
- **Peristiwa** — catatan simulasi; klik entri untuk melompat ke waktunya.

Peta (kiri): **tampilan 3D isometrik** — gerbang di dinding barat (sisi pendek, dekat
bay 01), lorong rak menjauh dari gerbang, ujung rak terlihat jauh di dalam gedung,
seperti render K_ONE. Rak back-to-back CA–CF (warna = persentase isi, garis biru =
pickface), staging STG1–8, penerimaan, **7 gerbang (G1–G7, 2 aktif secara bawaan —
kadang 3, ubah dengan `--docks`)**, halte antre truk (40 slot). Kursor menunjuk
 orang/truk/bin untuk detail; truk berstatus peristiwa: gate / docked / leaving.

Kontrol tampilan: **seret** untuk menggeser peta, **roda mouse** untuk zoom (di
posisi kursor), **klik ganda** untuk reset ke tampilan penuh.

## Playback

Putar/Jeda, 1×–64× (1× = 60 detik simulasi per detik nyata), scrubber, jam HH:MM.
Filter **Level rak** A–E menampilkan satu level; "Semua" menampilkan kelima level
A–E bertumpuk.

## Endpoint serve.py

| Route | Fungsi |
|---|---|
| `GET /` | viewer.html |
| `GET /out/<dir>/<file>` | artefak run (sim_log.json, kpis.json, trucks.csv) |
| `GET /api/runs` | daftar run di out/ |
| `POST /api/simulate` | body JSON `{pickers,checkers,stackers,forklifts,loaders,trucks,trucks_in,trucks_out,seed,schedule,arrival_order?}` → jalankan satu simulasi (503 bila masih ada yang berjalan) |
| `POST /api/client-error` | beacon error JS dari browser (masuk log server) |

Error JS apa pun ditampilkan sebagai banner merah di kiri-bawah — bila muncul,
salin teksnya (otomatis terkirim ke log server).

## Data

- `data/bins.csv`, `data/items.csv`, `data/stock.csv` diekstrak dari
  `supabase/seed.sql` repo `allocator_k-one` (stok buka 24 Sep 2026).
- **Pesanan nyata**: `.venv/bin/python extract_schedule.py <xlsx>` membaca sheet
  **"Schedule of the day"** (dua belas kolom order/shipment/Arrival Slot Time)
  dan menulis `data/schedule.json`. `wms_sim.py` memakai file itu otomatis bila ada;
  `--no-schedule` untuk pak jadwal sintetis. Line SKU yang belum ada di stok buka
  dilewati dan dilaporkan — muncul sebagai `short_cartons` di truck tersebut
  (per 16 Okt: 4 SKU pendek saat stok 24 Sep dipakai).
- Simulasi: `wms_sim.py` (SimPy, FEFO, kedatangan truk telat sesuai seed).
- Staf: picker, checker, pemuat; 2 forklift (level bawah) + 2 stacker reach-truck
  (level atas, C–E); tidak ada operator/controller/restocker terpisah.
- Skenario contoh urutan kedatangan 2,4,6,1,7,9 sudah tersimpan di `out/late-trucks/`
  — pilih lewat dropdown **Hasil**.
