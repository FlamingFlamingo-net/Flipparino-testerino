"""Desk console: a local page for reporting GE results and running refreshes without the chat.

  python tools/console.py            start the server on http://127.0.0.1:8766
  pythonw tools/console.py --open    start it (if not running) and open it as an app window (Edge/Chrome)

Only listens on 127.0.0.1. Every change goes through desk.py's own commands, so the console and
the chat write the ledger the same way.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
import desk  # noqa: E402

PORT = 8766
STATIC = ROOT / "console"
LOCK = threading.Lock()  # one ledger write at a time
NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}  # no console flash under pythonw
PY = sys.executable.replace("pythonw.exe", "python.exe")

# What the user can report per open offer, and when each choice applies.
ACTIONS = {
    "same":       "No change: still in the GE as planned",
    "done":       "Finished",                         # buy: bought and listed at the plan's sell price
    "done_sold":  "Bought AND already sold",          # buy only
    "unlisted":   "Bought, not listed yet",           # buy only
    "part":       "Partly filled, still in the GE",
    "aborted":    "Aborted (enter how many filled)",
    "not_placed": "Never placed",
    "price":      "Listed at a different price",
    "no_reprice": "Re-price not done (still at the old price)",  # CHANGE rows only
    "keep":       "Keep this price, stop suggesting changes",
}


def run(*args: str) -> str:
    """Run one desk.py command and return its output (raises on failure)."""
    p = subprocess.run([PY, str(TOOLS / "desk.py"), *args], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"}, **NO_WINDOW)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout).strip() or f"desk.py {' '.join(args)} failed")
    return p.stdout.strip()


def commands_for(row: dict, act: dict) -> list[list[str]]:
    """Translate one reported action into desk.py commands. Pure, so it's unit-tested.
    row: {n, side, filled, was_price}; act: {kind, qty?, price?, sell_price?}."""
    n, kind = str(row["n"]), act["kind"]
    qty, price = act.get("qty"), act.get("price")
    p = ["--price", str(price)] if price else []
    if kind == "same":
        return []
    if kind == "done":
        return [["fill", n, *p]]
    if kind == "unlisted":
        return [["fill", n, *p, "--no-sell"]]
    if kind == "done_sold":
        sp = act.get("sell_price")
        return [["fill", n, *p], ["@fill-last", *(["--price", str(sp)] if sp else [])]]
    if kind == "part":
        if qty is None:
            raise ValueError("enter the total filled so far")
        return [["fill", n, "--to", str(qty), *p]]
    if kind == "aborted":
        q = int(qty or 0)
        if q == 0 and not row.get("filled"):
            return [["cancel", n]]
        return [["fill", n, "--to", str(q), *p, "--close", "--no-sell"]]
    if kind == "not_placed":
        return [["cancel", n]]
    if kind == "price":
        if not price:
            raise ValueError("enter the price it's listed at")
        return [["edit", n, "--price", str(price)]]
    if kind == "no_reprice":
        if not row.get("was_price"):
            raise ValueError("this offer has no earlier price")
        return [["edit", n, "--price", str(row["was_price"])]]
    if kind == "keep":
        return [["edit", n, *p, "--pin"]]
    raise ValueError(f"unknown action {kind}")


def next_arg(when: dict) -> list[str]:
    """{"mode": "normal"|"watch"|"hours"|"at"|"away", "value": ...} -> plan flags."""
    mode, v = when.get("mode", "normal"), when.get("value")
    if mode == "watch":
        return ["--next", "0"]
    if mode == "hours":
        t = desk.now() + desk.dt.timedelta(hours=float(v))
        return ["--next", f"{t:%H:%M}"]
    if mode == "at":
        return ["--next", str(v)]
    if mode == "away":
        return ["--away", str(v)]
    return []


def open_rows() -> dict:
    """Open offers from the latest plan, numbered as desk.py expects."""
    s = desk.load_state()
    if not s["plans"]:
        return {"rows": [], "plan": None}
    plan = s["plans"][-1]
    by_oid = {r["oid"]: r for r in desk.plan_rows(s, plan)}
    num = {oid: int(n) for n, oid in plan["map"].items()}
    rows = []
    for o in desk.open_orders(s):
        r = by_oid.get(o["id"], {})
        rows.append({
            "n": num.get(o["id"]), "slot": o.get("slot"), "item": o["name"], "side": o["side"],
            "qty": o["qty"], "filled": desk.filled(o), "price": o["price"], "verb": r.get("verb", "KEEP"),
            "was_price": r.get("was_price"), "then_sell": o.get("target_sell") if o["side"] == "buy" else None,
            "machine": o.get("strategy") == "machine", "pinned": bool(o.get("pinned")),
        })
    rows.sort(key=lambda r: (r["slot"] or 99, r["n"] or 999))
    real = sum(x["profit"] for x in s["realized"])
    recent = [{"id": p["id"], "at": p["at"]} for p in s["plans"][-4:-1][::-1]
              if (desk.LEDGER / "plans" / f"{p['id']}.html").exists()]
    return {"plan": plan["id"], "recent": recent, "at": plan["at"], "cash": s["cash"], "realised": real,
            "capital": s["cfg"].get("capital"), "rows": rows, "actions": ACTIONS}


def apply_report(actions: list[dict]) -> list[str]:
    rows = {r["n"]: r for r in open_rows()["rows"]}
    log = []
    for act in actions:
        row = rows.get(int(act["n"]))
        if not row:
            raise ValueError(f"trade {act['n']} is no longer open; reload the page")
        for cmd in commands_for(row, act):
            if cmd[0] == "@fill-last":  # the sell that `fill` just recorded for a finished buy
                last = log[-1] if log else ""
                k = last.split("recorded as trade ")[-1].split(":")[0] if "recorded as trade " in last else None
                if not k:
                    raise RuntimeError(f"{row['item']}: no sell offer was recorded to mark as sold")
                cmd = ["fill", k, *cmd[1:]]
            log.append(run(*cmd))
    return log


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(STATIC), **k)

    def log_message(self, *_):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/board" or path.startswith("/plans/"):
            f = desk.LEDGER / desk.BOARD
            if path.startswith("/plans/"):  # an earlier plan's panel: /plans/<plan id>
                pid = path.rsplit("/", 1)[-1]
                if pid not in {p["id"] for p in desk.load_state()["plans"]}:
                    return self.send_json({"error": "unknown plan"}, 404)
                f = desk.LEDGER / "plans" / f"{pid}.html"
            body = f.read_bytes() if f.exists() else b"No board yet: run a refresh."
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            if path == "/api/state":
                return self.send_json(open_rows())
            if path == "/api/totals":
                return self.send_json({"text": run("totals")})
            if path == "/api/machines":
                return self.send_json({"text": run("machines").split("## Shopping list", 1)[-1].strip()})
        except Exception as e:  # noqa: BLE001
            return self.send_json({"error": str(e)}, 500)
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            with LOCK:
                if path == "/api/report":
                    log = apply_report(data.get("actions", []))
                    out = {"log": log}
                    if data.get("refresh"):
                        out["plan"] = run("plan", "--review-all", *next_arg(data.get("when", {})))
                        out["log"].append(run_backup())
                    return self.send_json(out)
                if path == "/api/bank":
                    spec = f'{data["kind"]}={data["item"]}={int(data["qty"])}'
                    return self.send_json({"text": run("machines", "--bank", spec).split("## Shopping list", 1)[-1].strip()})
        except Exception as e:  # noqa: BLE001
            return self.send_json({"error": str(e)}, 400)
        self.send_json({"error": "not found"}, 404)


def run_backup() -> str:
    p = subprocess.run([PY, str(TOOLS / "backup.py")], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                       **NO_WINDOW)
    return (p.stdout or p.stderr).strip()


def running() -> bool:
    with socket.socket() as sk:
        sk.settimeout(0.3)
        return sk.connect_ex(("127.0.0.1", PORT)) == 0


def open_window():
    url = f"http://127.0.0.1:{PORT}/"
    for exe in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                shutil.which("msedge") or "", shutil.which("chrome") or ""):
        if exe and Path(exe).exists():
            subprocess.Popen([exe, f"--app={url}", "--window-size=1400,950"])
            return
    import webbrowser
    webbrowser.open(url)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--open", action="store_true", help="open the console as an app window")
    a = ap.parse_args()
    if running():
        if a.open:
            open_window()
        return
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    if a.open:
        threading.Thread(target=lambda: (time.sleep(0.5), open_window()), daemon=True).start()
    print(f"Desk console on http://127.0.0.1:{PORT}/  (Ctrl+C to stop)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
