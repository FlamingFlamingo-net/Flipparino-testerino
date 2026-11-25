#!/usr/bin/env python3
"""Weekly report: how the desk and the invention machines did over one week (Tuesday 18:00 to Tuesday 18:00,
local time). No network, no tokens: it reads ledger/state.json only.

  python tools/weekly.py                 the week that ended at the most recent Tuesday 18:00
  python tools/weekly.py --end 2026-11-24   a given Tuesday (18:00)
Writes ledger/weekly/week-<end date>.md and .json (the next report compares against the .json).
"""
from __future__ import annotations

import argparse, datetime as dt, json, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import desk  # noqa: E402

TUESDAY, HOUR = 1, 18


def week_end(ref: dt.datetime) -> dt.datetime:
    d = ref.replace(hour=HOUR, minute=0, second=0, microsecond=0) - dt.timedelta(days=(ref.weekday() - TUESDAY) % 7)
    return d if d <= ref else d - dt.timedelta(days=7)


def m(n: float) -> str:
    return f"{n / 1e6:+.1f}m" if abs(n) >= 1e5 else f"{n:+,.0f}"


def report(s: dict, end: dt.datetime) -> tuple[str, dict]:
    start = end - dt.timedelta(days=7)
    inwin = lambda iso: start <= dt.datetime.fromisoformat(iso) < end  # noqa: E731
    rows = [r for r in s["realized"] if inwin(r["at"])]
    profit = sum(r["profit"] for r in rows)
    won = [r for r in rows if r["profit"] > 0]
    lost = [r for r in rows if r["profit"] <= 0]
    cap = s.get("capital", 200_000_000)
    L = [f"# Weekly report: {start:%a %d %b %H:%M} to {end:%a %d %b %H:%M}", ""]

    # ---- sales
    L.append("## Sales")
    L.append(f"- **Realised profit {m(profit)}** ({100 * profit / cap:+.1f}% of the {cap / 1e6:.0f}m capital) from "
             f"**{len(rows)} trades**, {len(won)} won and {len(lost)} lost"
             + (f" (win rate {100 * len(won) / len(rows):.0f}%)." if rows else "."))
    if rows:
        days = defaultdict(float)
        for r in rows:
            days[dt.datetime.fromisoformat(r["at"]).strftime("%a")] += r["profit"]
        L.append(f"- Average trade {m(profit / len(rows))}; about {m(profit / 7)} a day. "
                 + "By day: " + ", ".join(f"{d} {m(v)}" for d, v in days.items()) + ".")
        by = defaultdict(lambda: [0, 0.0])
        for r in rows:
            by[r["name"]][0] += 1
            by[r["name"]][1] += r["profit"]
        top = sorted(by.items(), key=lambda kv: -kv[1][1])
        L += ["", "| Item | Trades | Profit | Share |", "|---|---|---|---|"]
        for name, (n, p) in top[:8]:
            L.append(f"| {name} | {n} | {m(p)} | {100 * p / profit:.0f}% |" if profit else f"| {name} | {n} | {m(p)} | |")
        if len(top) > 8:
            rest = sum(p for _, (_, p) in top[8:])
            L.append(f"| {len(top) - 8} other items | {sum(n for _, (n, _) in top[8:])} | {m(rest)} | |")
        if lost:
            L += ["", "**Losing trades:** " + "; ".join(f"{r['name']} x{r['qty']:,} {m(r['profit'])}" for r in
                                                      sorted(lost, key=lambda r: r["profit"])[:5])]
        best = max(rows, key=lambda r: r["profit"])
        L.append(f"- Best single trade: {best['name']} x{best['qty']:,} {m(best['profit'])}.")

    # ---- money
    cash = s["cash"]
    held = sum(h["cost"] for h in s["holdings"].values() if h["qty"] > 0)
    buys = sum(desk.remaining(o) * o["price"] for o in desk.open_orders(s) if o["side"] == "buy"
               and o.get("strategy") != "machine")
    total = cash + held + buys
    prev_file = None
    wk = Path(desk.LEDGER) / "weekly"
    prevs = sorted(wk.glob("week-*.json")) if wk.exists() else []
    prevs = [p for p in prevs if p.stem != f"week-{end:%Y-%m-%d}"]
    prev = json.loads(prevs[-1].read_text(encoding="utf-8")) if prevs else None
    L += ["", "## Money"]
    L.append(f"- Total money now **{total / 1e6:,.1f}m**: cash {cash / 1e6:,.1f}m, items held at cost {held / 1e6:,.1f}m, "
             f"tied in buy offers {buys / 1e6:,.1f}m. Idle cash is {100 * cash / total:.0f}% of the total.")
    if prev:
        L.append(f"- Last report ({prev['end'][:10]}): total {prev['total'] / 1e6:,.1f}m, realised {prev['realised_total'] / 1e6:,.1f}m. "
                 f"Total money moved **{m(total - prev['total'])}** (includes any deposits).")
    realised_total = sum(r["profit"] for r in s["realized"])
    L.append(f"- Realised profit since the start: {realised_total / 1e6:,.1f}m ({100 * realised_total / cap:.1f}% on {cap / 1e6:.0f}m).")

    # ---- machines
    L += ["", "## Machines"]
    ms = s["machines"]
    w = ms.get("week") or {}
    wk_machine = w if w and dt.datetime.fromisoformat(w["refill"]) <= end + dt.timedelta(hours=1) and \
        dt.datetime.fromisoformat(w["refill"]) > start else None
    if wk_machine is None:
        for h in ms.get("history", []):
            if h and start < dt.datetime.fromisoformat(h["refill"]) <= end + dt.timedelta(hours=1):
                wk_machine = h
    setup = ms.get("setup", {})
    L.append("- Setup: " + (", ".join(f"{k}x {desk.mc.MACHINES[x]['name']}" for x, k in setup.items() if k) or "none")
             + " (the load at the end of this week is the one before: see the leftovers below).")
    if wk_machine:
        L.append(f"- **Spent on machine inputs: {wk_machine.get('spent', 0) / 1e6:.1f}m** (separate machine pot).")
        L += ["", "| Input | Target | Bought | Left |", "|---|---|---|---|"]
        for t in wk_machine.get("targets", []):
            b = wk_machine.get("bought", {}).get(t["item"], 0)
            L.append(f"| {t['item']} | {t['qty']:,} | {b:,} | {max(t['qty'] - b, 0):,} |")
        bank = wk_machine.get("bank")
        if bank:
            L.append("")
            L.append("Bank leftovers you reported after the last load: " + "; ".join(
                f"{i} {q:,}" for kind in bank.values() for i, q in kind.items()) + ".")
    else:
        L.append("- No machine week was recorded for this period.")
    rep_f = Path(desk.LEDGER) / "machines.json"
    if rep_f.exists():
        mr = json.loads(rep_f.read_text(encoding="utf-8")).get("machines", {})
        try:  # estimate for the setup recorded in the ledger
            ns = {k: min(n, desk.mc.MACHINES[k]["max"]) for k, n in setup.items() if n}  # patch: max 1 alchemiser
            bad = sum(mr[k]["fill"][str(n)]["worst_week"] for k, n in ns.items())
            typ = sum(mr[k]["fill"][str(n)]["typical_week"] for k, n in ns.items())
            L.append(f"- **Expected machine profit** for the ledger's setup: about {m(bad)} in a bad week, {m(typ)} typical "
                     "(the planner's estimate; real output depends on what you load and sell, and isn't in the ledger).")
        except KeyError:
            pass
    ext = s.get("external_stock") or {}
    live_ext = {k: v for k, v in ext.items() if v.get("qty")}
    if live_ext:
        L += ["", "## Outside the ledger", "- " + "; ".join(f"{k} {v['qty']:,}" for k, v in live_ext.items()) + " (your own stock)."]
    data = {"end": end.isoformat(), "total": total, "cash": cash, "realised_week": profit, "realised_total": realised_total,
            "trades": len(rows)}
    return "\n".join(L) + "\n", data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", help="YYYY-MM-DD of the Tuesday (18:00 local) the week ends on")
    ap.add_argument("--no-save", action="store_true")
    a = ap.parse_args()
    now = desk.now()
    end = (dt.datetime.fromisoformat(a.end).replace(hour=HOUR, tzinfo=now.tzinfo) if a.end else week_end(now))
    s = desk.load_state()
    text, data = report(s, end)
    if not a.no_save:
        d = Path(desk.LEDGER) / "weekly"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"week-{end:%Y-%m-%d}.md").write_text(text, encoding="utf-8")
        (d / f"week-{end:%Y-%m-%d}.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
