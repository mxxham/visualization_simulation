#!/usr/bin/env python3
"""
serve.py: stdlib-only server for the wms-sim viewer.
  python3 serve.py             -> http://localhost:8000/viewer.html
  python3 serve.py --port 9100

Endpoints:
  GET  /                        -> viewer.html (static file service for this directory)
  GET  /out/<dir>/<file>        -> run artifacts (sim_log.json, kpis.json, trucks.csv)
  GET  /api/runs                -> JSON list of run dirs under out/
  POST /api/simulate            -> body: {name?, overrides?, pickers?, checkers?,
                                   loaders?, forklifts?, stackers?, docks?, seed?,
                                   trucks?, trucks_in?, trucks_out?, schedule?}
                                   trucks_in / trucks_out set the day's inbound and
                                   outbound truck counts (0-20; -1 or absent = auto);
                                   runs wms_sim.py once (503 while busy); any Config
                                   key may be passed via overrides (e.g. timings
                                   t_check_pallet, t_load_pallet, pick_lead_min);
                                   writes out/<name|run-<UTC>>/, returns {ok, dir,...}
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
VENV_PY = ROOT / ".venv" / "bin" / "python"
PY = str(VENV_PY) if VENV_PY.exists() else "python3"
LOCK = threading.Lock()


def clamp(value, lo, hi):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


def run_sim(params: dict) -> dict:
    if params.get("name"):
        safe = "".join(c for c in str(params["name"]) if c.isalnum() or c in "-_")
        run_id = safe[:40] or "run-" + time.strftime("%H%M%S", time.gmtime())
    else:
        run_id = "run-" + time.strftime("%H%M%S", time.gmtime())
    cmd = [PY, "wms_sim.py", "--out", f"out/{run_id}"]
    # only pass what the caller asked for - omitting a flag keeps the Config default
    for flag, key, lo, hi in (
        ("--pickers", "pickers", 1, 8),
        ("--checkers", "checkers", 1, 8),
        ("--stackers", "stackers", 1, 6),
        ("--forklifts", "forklifts", 1, 6),
        ("--loaders", "loaders", 1, 12),
        ("--docks", "docks", 1, 7),
    ):
        if params.get(key) is not None:
            cmd += [flag, str(clamp(params.get(key), lo, hi))]
    if params.get("seed") is not None:
        cmd += ["--seed", str(clamp(params.get("seed"), 1, 10**6))]
    if params.get("trucks") is not None:
        cmd += ["--trucks", str(clamp(params.get("trucks"), 1, 20))]
    for flag, key in (("--trucks-in", "trucks_in"), ("--trucks-out", "trucks_out")):
        if params.get(key) is not None:
            cmd += [flag, str(clamp(params.get(key), -1, 20))]
    ov = params.get("overrides")
    if isinstance(ov, dict) and ov:
        cmd += ["--overrides", json.dumps(ov)]
    if not params.get("schedule", True):
        cmd += ["--no-schedule"]
    ids = [
        str(clamp(x, 1, 20))
        for x in (params.get("arrival_order") or [])
        if str(x).strip().isdigit()
    ]
    if ids:
        cmd += ["--arrival-order", ",".join(ids)]
    print("[serve] exec:", " ".join(cmd), flush=True)
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=1200)
    took = round(time.time() - t0, 1)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-1500:]
        print("[serve] FAILED:", tail, flush=True)
        return {"ok": False, "error": tail or f"exit {proc.returncode}", "dir": run_id}
    print(f"[serve] done in {took}s -> out/{run_id}", flush=True)
    return {
        "ok": True,
        "dir": run_id,
        "log": f"/out/{run_id}/sim_log.json",
        "kpis": f"/out/{run_id}/kpis.json",
        "seconds": took,
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/":
                return self._file(ROOT / "viewer.html", "text/html; charset=utf-8")
            if path == "/api/runs":
                runs = sorted(
                    p.name for p in OUT.iterdir() if (p / "sim_log.json").exists()
                )
                return self._json({"runs": runs})
            target = (ROOT / urllib.parse.unquote(path.lstrip("/"))).resolve()
            if not str(target).startswith(str(ROOT)):
                return self._code(403)
            return self._file(target, self._mime(target))
        except FileNotFoundError:
            return self._code(404)
        except Exception as e:  # keep the server alive
            return self._json({"error": repr(e)}, 500)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/client-error":
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                data = json.loads(self.rfile.read(length).decode() or "{}")
            except Exception:
                data = {}
            print(
                "[client-error]",
                data.get("ua", "?"),
                "::",
                data.get("msg", ""),
                flush=True,
            )
            return self._json({"ok": True})
        if path != "/api/simulate":
            return self._code(404)
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            params = json.loads(body.decode() or "{}")
        except json.JSONDecodeError:
            return self._json({"ok": False, "error": "bad JSON"}, 400)
        if not LOCK.acquire(blocking=False):
            return self._json(
                {"ok": False, "error": "busy: one simulation at a time"}, 503
            )
        try:
            reply = run_sim(params)
            return self._json(reply, 200 if reply.get("ok") else 500)
        except subprocess.TimeoutExpired:
            return self._json({"ok": False, "error": "timed out after 20 min"}, 504)
        finally:
            LOCK.release()

    @staticmethod
    def _mime(p: Path) -> str:
        return {
            ".html": "text/html; charset=utf-8",
            ".json": "application/json",
            ".csv": "text/csv",
            ".py": "text/plain; charset=utf-8",
            ".md": "text/plain; charset=utf-8",
        }.get(p.suffix, "application/octet-stream")

    def _file(self, p: Path, mime: str):
        if not p.is_file():
            return self._code(404)
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _code(self, c):
        self.send_response(c)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):  # quiet
        pass


def main():
    port = 8000
    if "-p" in sys.argv or "--port" in sys.argv:
        i = sys.argv.index("-p") if "-p" in sys.argv else sys.argv.index("--port")
        port = clamp(sys.argv[i + 1], 1025, 65535)
    OUT.mkdir(exist_ok=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
