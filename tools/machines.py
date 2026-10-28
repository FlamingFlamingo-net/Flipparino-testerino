#!/usr/bin/env python3
"""Invention machines: which maxed machine and which inputs give the most CONSISTENT weekly profit.

Every method is put on the same footing: one machine, one week (the user refills on Tuesday 18:00),
profit after GE tax, machine charge and secondary inputs. Consistency is measured on the last 90 days
of live GE prices (daily averages): for every day we compute what the week would have earned if you
had bought/sold at that day's average prices. We rank by the WORST-LIKELY week (10th percentile),
not the average, and drop anything that lost money on more than 5 % of those days.

Feasibility uses real traded volume, assuming COMPETITORS (50) equal buyers want the same item:
we can count on 1/51 of a slow week's sellers (25th-percentile day x 7), and the same share of the
output's buyers. 50 comes from the user's real water staff buying times (see COMPETITORS). Each machine type runs ONE input item for the whole week (Tuesday 18:00 reset to the
next); if that item can't fill the machine, the machine runs part of the week and the profit says so.

  python tools/machines.py                 full report (all machines, best inputs, buy timing)
  python tools/machines.py --json          same, machine-readable

Machine data: runescape.wiki/w/Machines (maxed tiers only). Simple-parts disassembly data:
tools/simple_parts.json (from Calculator:Disassembly_by_material/Simple; refresh with --update-simple).
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import re
import statistics
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rsmarket as rm  # noqa: E402

HERE = Path(__file__).resolve().parent
SIMPLE = HERE / "simple_parts.json"
TAX = 0.02
WEEK_H = 7 * 24
DC = "Divine charge"           # 3,000 machine charge each
EMPTY = "Divine charge (empty)"  # 20 simple parts each
JCR = 0.80                      # junk chance reduction 9 (Invention 105): junk x 0.8

# Maxed tier of each machine (wiki: Machines). per_h = items per hour, mc_h = machine charge per hour.
MACHINES = {
    "alch":   {"name": "Alchemiser mk. II", "power": 55, "per_h": 25, "mc_h": 150, "cap": 5_000, "max": 2},
    "disasm": {"name": "Auto disassembler mk. II", "power": 40, "per_h": 60, "mc_h": 230, "cap": 13_000, "max": 6},
    "tanner": {"name": "Optimised hide tanner", "power": 50, "per_h": 140, "mc_h": 840, "cap": 30_000, "max": 6},
    "plank":  {"name": "High capacity plank maker", "power": 65, "per_h": 40, "mc_h": 600, "cap": 7_500, "max": 6},
    "potion": {"name": "Partial potion producer DX", "power": 50, "per_h": 40, "mc_h": 210, "cap": 13_000, "max": 6},
}
HIDES = [("Cowhide", "Hard leather"), ("Snake hide", "Snakeskin"), ("Snake hide (swamp)", "Snakeskin"),
         ("Green dragonhide", "Green dragon leather"), ("Blue dragonhide", "Blue dragon leather"),
         ("Red dragonhide", "Red dragon leather"), ("Black dragonhide", "Black dragon leather"),
         ("Royal dragonhide", "Royal dragon leather"), ("Undead dragonhide", "Undead dragon leather"),
         ("Dinosaur hide", "Dinosaur leather"), ("Apex hide", "Apex leather")]
LOGS = [("Logs", "Plank"), ("Oak logs", "Oak plank"), ("Teak logs", "Teak plank"), ("Mahogany logs", "Mahogany plank")]

# How much we can realistically buy/sell in a week
BUY_WINDOWS = 14     # 4-hour buy-limit windows in a week (2 GE visits a day x 7 days)
COMPETITORS = 50     # "equal buyers" we share a market with. Calibrated from the user's water staff buys:
                     # 8,400 took ~1.5 days (≈26 equal buyers), 4 days at worst (≈54) -> 50 is the cautious end
VOL_PCTL = 0.25      # "slow week" = the 25th-percentile day of the last 90, times 7
DAYS = 90            # consistency window
MAX_LOSS_SHARE = 0.05
MIN_ITEM_WEEK = 500_000  # an extra input must add this much a week, or it isn't worth a GE slot
CAP_RATE = 0.02      # cash tied in machine stock could earn ~2 %/week flipping: charge it, so cheap inputs win ties
CURRENT = {"alch": ["Water battlestaff"], "disasm": ["Yak-hide"]}  # what the user runs today (always analysed)


# ---------- data ----------
def update_simple() -> int:
    """Re-scrape the wiki's Simple parts calculator into simple_parts.json."""
    url = ("https://runescape.wiki/api.php?action=parse&page=Calculator:Disassembly_by_material/Simple"
           "&prop=text&format=json")
    raw = rm.get_json(url, timeout=120)["parse"]["text"]["*"]
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", raw, re.S):
        sv = [m.group(1) if m else None for m in
              (re.search(r'data-sort-value="([^"]*)"', a) for a, _ in re.findall(r"<td([^>]*)>(.*?)</td>", row, re.S))]
        if len(sv) < 10:
            continue
        try:
            out.append({"name": html.unescape(sv[1]), "mats": float(sv[2]), "junk": float(sv[3]) / 100,
                        "cost": float(sv[4] or 0), "raw": float(sv[7]) / 100})
        except (TypeError, ValueError):
            continue
    SIMPLE.write_text(json.dumps(out, indent=0), encoding="utf-8")
    return len(out)


def simple_rate(r) -> float:
    """Expected simple parts per disassemble with junk chance reduction 9."""
    return r["mats"] * (1 - r["junk"] * JCR) * r["raw"]


def series_prices(points):
    """[(ts, avg_low, avg_high, low_vol, high_vol)] with gaps carried forward."""
    out, lo, hi = [], None, None
    for p in sorted(points or [], key=lambda p: p["timestamp"]):
        lo = p.get("avgLowPrice") or lo
        hi = p.get("avgHighPrice") or hi
        if lo or hi:
            out.append((p["timestamp"], lo or hi, hi or lo, p.get("lowPriceVolume") or 0, p.get("highPriceVolume") or 0))
    return out


def load_daily(ids, workers: int = 6) -> dict:
    """Last DAYS days of daily average prices + volumes from the live-price API (lookback=1y is daily)."""
    from concurrent.futures import ThreadPoolExecutor

    def one(i):
        try:
            pts = rm._retry(lambda: rm.get_json(f"{rm.RT}/timeseries?lookback=1y&id={i}"))["data"]
        except Exception:
            return i, None
        if not pts:
            return i, []
        cut = max(p["timestamp"] for p in pts) - DAYS * 86400
        return i, [p for p in pts if p["timestamp"] > cut]
    with ThreadPoolExecutor(workers) as ex:
        return dict(ex.map(one, list(ids)))


def weekly_flow(pts):
    """A slow week's (instant-sell volume, instant-buy volume): 25th-percentile day x 7.
    Instant-sells are who fills our buy offers; instant-buys are who fills our sell offers."""
    if len(pts) < 2:
        return 0, 0
    return (pctl([p[3] for p in pts], VOL_PCTL) * 7, pctl([p[4] for p in pts], VOL_PCTL) * 7)


def pctl(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    f = math.floor(k)
    return xs[f] + (xs[min(f + 1, len(xs) - 1)] - xs[f]) * (k - f)


# ---------- per-item economics ----------
def candidates(mk):
    by = mk["by_name"]
    c = {k: [] for k in MACHINES}
    for it in mk["items"].values():  # alchemiser: anything worth < 500k with a high-alch value
        if it["highalch"] and it.get("low") and 0 < it["low"] < 500_000 and it["highalch"] > it["low"]:
            c["alch"].append({"in": it["name"], "out": None, "alch": it["highalch"]})
    if SIMPLE.exists():
        for r in json.loads(SIMPLE.read_text(encoding="utf-8")):
            it = by.get(r["name"])
            if not it or not it.get("low"):
                continue
            if r["cost"] and r["cost"] > 1.5 * max(it["low"], it.get("high") or 0):
                continue  # disassembled in batches: the machine counts each item, not the batch
            rate = simple_rate(r)
            if rate >= 0.3:
                c["disasm"].append({"in": r["name"], "out": EMPTY, "ratio": rate / 20})
    for a, b in HIDES:
        if a in by and b in by:
            c["tanner"].append({"in": a, "out": b, "ratio": 1})
    for a, b in LOGS:
        if a in by and b in by:
            c["plank"].append({"in": a, "out": b, "ratio": 1})
    for name in by:
        if name.startswith("Clean "):
            unf = name[6:].capitalize() + " potion (unfinished)"
            if unf in by:
                c["potion"].append({"in": name, "out": unf, "ratio": 1, "vial": True})
    return c


def robust_low(it):
    """Median of latest / 1h / 24h instant-sell price: one stray trade can't fake a bargain."""
    return rm._med([it.get("low"), it.get("l1"), it.get("l24")]) or 0


def robust_high(it):
    return rm._med([it.get("high"), it.get("h1"), it.get("h24")]) or 0


def quick_profit(m, cand, mk, dc_each, nat, vial):
    """Profit per input at today's prices, for shortlisting before the history download."""
    it = mk["by_name"][cand["in"]]
    buy = robust_low(it)
    cost = buy + MACHINES[m]["mc_h"] / MACHINES[m]["per_h"] / 3000 * dc_each
    if m == "alch":
        return cand["alch"] - buy - nat - MACHINES[m]["mc_h"] / MACHINES[m]["per_h"] / 3000 * dc_each
    out = robust_high(mk["by_name"][cand["out"]])
    if cand.get("vial"):
        cost += vial
    return cand["ratio"] * (out - rm.ge_tax(out, TAX, cand["out"])) - cost


def history_profit(m, cand, S, dc_pts, nat_pts, vial_pts):
    """Per-input profit at every 6-hour point of the last 30 days (buy at avg low, sell at avg high)."""
    inp = {p[0]: p for p in S[cand["in"]]}
    outp = {p[0]: p for p in S[cand["out"]]} if cand.get("out") else {}
    dcs, nats, vials = ({p[0]: p for p in x} for x in (dc_pts, nat_pts, vial_pts))
    mc_each = MACHINES[m]["mc_h"] / MACHINES[m]["per_h"] / 3000
    res, last = [], {}
    for ts in sorted(inp):
        for key, src in (("i", inp), ("o", outp), ("d", dcs), ("n", nats), ("v", vials)):
            if ts in src:
                last[key] = src[ts]
        if "i" not in last or "d" not in last:
            continue
        cost = last["i"][1] + mc_each * last["d"][1]
        if m == "alch":
            if "n" not in last:
                continue
            res.append(cand["alch"] - cost - last["n"][1])
        else:
            if "o" not in last:
                continue
            sell = last["o"][2] - 1
            if cand.get("vial"):
                if "v" not in last:
                    continue
                cost += last["v"][1]
            res.append(cand["ratio"] * (sell - rm.ge_tax(sell, TAX, cand["out"])) - cost)
    return res


def buy_timing(pts):
    """Is one weekday reliably cheaper? Each day's avg low vs the mean of the 7 days around it.
    Returns the cheapest weekday if it was cheaper in at least 70 % of weeks, else None."""
    if len(pts) < 60 or weekly_flow(pts)[0] < 5_000:  # thin items: any "pattern" is noise
        return None
    by_day = {}
    for i, p in enumerate(pts):
        window = [q[1] for q in pts[max(0, i - 3):i + 4]]
        if len(window) < 7:
            continue
        wd = dt.datetime.fromtimestamp(p[0], dt.timezone.utc).weekday()
        by_day.setdefault(wd, []).append(p[1] / statistics.mean(window) - 1)
    best = None
    for wd, devs in by_day.items():
        if len(devs) < 8:
            continue
        avg, cheaper = statistics.mean(devs), sum(d < 0 for d in devs) / len(devs)
        if cheaper >= 0.70 and (best is None or avg < best[1]):
            best = (wd, avg, cheaper, len(devs))
    if not best or best[1] > -0.003:
        return None
    wd, avg, share, n = best
    return {"day": "Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split()[wd], "hours": "",
            "saving": avg, "weeks": f"{round(share * n)}/{n}"}


def obtainable(it, pts_in, pts_out, ratio):
    """Inputs per week ONE player can count on: buy limit x windows, and a fair share of a slow week's
    sellers when we and COMPETITORS other players all want the same item (same for the output's buyers)."""
    sold_wk, _ = weekly_flow(pts_in)
    share = 1 / (1 + COMPETITORS)
    q = min((it["limit"] or 10**9) * BUY_WINDOWS, sold_wk * share)
    if pts_out is not None:
        _, out_bought = weekly_flow(pts_out)
        q = min(q, out_bought * share / max(ratio, 1e-9))
    return int(q)


# ---------- analysis ----------
def analyse(shortlist: int = 40, budget: float = math.inf):
    mk = rm.load_market()
    by = mk["by_name"]
    dc_each = rm._med([by[DC]["low"], by[DC]["l1"], by[DC]["l24"]])
    nat = rm._med([by["Nature rune"]["low"], by["Nature rune"]["l1"], by["Nature rune"]["l24"]])
    vial = by["Vial of water"]["low"] or 0
    cands = candidates(mk)
    short = {}
    for m, lst in cands.items():
        def weekly_qty(c):  # rough inputs/week we could get, from limit and the last day's sellers
            it = by[c["in"]]
            return min((it["limit"] or 10**7) * BUY_WINDOWS, it["lv24"] * 7 / (1 + COMPETITORS), MACHINES[m]["per_h"] * WEEK_H)
        scored = [(quick_profit(m, c, mk, dc_each, nat, vial), c) for c in lst]
        scored = [x for x in scored if x[0] > 0 and weekly_qty(x[1]) >= 100]
        scored.sort(key=lambda x: -x[0] * weekly_qty(x[1]))
        short[m] = [c for _, c in scored[:shortlist]]
        for name in CURRENT.get(m, []):  # always compare against what the user runs now
            if not any(c["in"] == name for c in short[m]):
                short[m] += [c for c in lst if c["in"] == name]
    names = {DC, "Nature rune", "Vial of water"}
    for lst in short.values():
        for c in lst:
            names.add(c["in"])
            if c.get("out"):
                names.add(c["out"])
    raw = load_daily([by[n]["id"] for n in names])
    S = {n: series_prices(raw.get(by[n]["id"])) for n in names}
    missing = sum(1 for n in names if not S[n])
    report = {"at": dt.datetime.now().astimezone().isoformat(timespec="minutes"), "dc_each": dc_each,
              "nature": nat, "missing_history": missing, "machines": {}}
    for m, lst in short.items():
        mach = MACHINES[m]
        per_week = mach["per_h"] * WEEK_H
        rows = []
        for c in lst:
            if not S.get(c["in"]) or (c.get("out") and not S.get(c["out"])):
                continue
            hist = history_profit(m, c, S, S[DC], S["Nature rune"], S["Vial of water"])
            if len(hist) < 60:
                continue
            loss = sum(h <= 0 for h in hist) / len(hist)
            it = by[c["in"]]
            q = obtainable(it, S[c["in"]], S[c["out"]] if c.get("out") else None, c.get("ratio", 1))
            if q < 100 and c["in"] not in CURRENT.get(m, []):
                continue
            rows.append({
                "item": c["in"], "out": c.get("out"), "now": round(quick_profit(m, c, mk, dc_each, nat, vial), 1),
                "p10": round(pctl(hist, 0.10), 1), "median": round(pctl(hist, 0.5), 1),
                "loss_share": round(loss, 3), "per_week_max": q, "limit": it["limit"],
                "buy_at": math.ceil(robust_low(it)) + 1, "timing": buy_timing(S[c["in"]]),
                "ratio": c.get("ratio"), "current": c["in"] in CURRENT.get(m, []),
                "days": len(hist), "vol_day": round(pctl([p[3] for p in S[c["in"]]], VOL_PCTL)),
                "out_vol_day": round(pctl([p[4] for p in S[c["out"]]], VOL_PCTL)) if c.get("out") else None,
            })
            rows[-1]["adj"] = round(rows[-1]["p10"] - CAP_RATE * rows[-1]["buy_at"], 1)
        safe = [r for r in rows if r["loss_share"] <= MAX_LOSS_SHARE and r["adj"] > 0 and r["per_week_max"] >= 100]
        safe.sort(key=lambda r: -r["adj"])
        cur = [r for r in rows if r["current"]]
        report["machines"][m] = {**mach, "per_week": per_week, "items": safe[:12], "rejected": len(rows) - len(safe),
                                 "current": cur}
    for m, d in report["machines"].items():
        # one input item per machine type for the whole week (user: no mixes, too much micro)
        d["fill"] = {k: fill_plan(d["items"], d["per_week"] * k, 1) for k in range(1, d["max"] + 1)}
    report["setups"] = best_setups(report, budget=budget)
    return report


def fill_plan(rows, need, max_items):
    """Best mix of up to `max_items` inputs (3 input slots per machine) for the worst-week profit."""
    from itertools import combinations
    best = None
    pool = rows[:12]
    for k in range(1, min(max_items, len(pool)) + 1):
        for combo in combinations(pool, k):
            p = _fill(combo, need, max_items)
            if best is None or p["score"] > best["score"]:
                best = p
    return best or _fill([], need, max_items)


def _fill(rows, need, max_items):
    plan, left = [], need
    for r in sorted(rows, key=lambda r: -r["adj"]):
        if left <= 0 or len(plan) >= max_items:
            break
        q = min(left, r["per_week_max"])
        q = q // 100 * 100 if q >= 100 else q
        if q <= 0 or (len(rows) > 1 and (q < 100 or q * r["p10"] < MIN_ITEM_WEEK)):  # crumbs aren't worth a slot
            continue
        plan.append({"item": r["item"], "qty": q, "p10": r["p10"], "median": r["median"], "buy_at": r["buy_at"],
                     "timing": r["timing"]})
        left -= q
    worst = sum(p["qty"] * p["p10"] for p in plan)
    typical = sum(p["qty"] * p["median"] for p in plan)
    cash = sum(p["qty"] * p["buy_at"] for p in plan)
    return {"items": plan, "filled": need - max(left, 0), "need": need, "worst_week": round(worst),
            "typical_week": round(typical), "cash": cash, "score": round(worst - CAP_RATE * cash)}


def best_setups(report, hotspots: int = 6, skip=("potion",), budget: float = math.inf):
    """Best machine combination for 160 power (jumbo generator) and 200 (with the tier-3 Fort workshop)."""
    ms = [m for m in report["machines"] if m not in skip]
    out = {}
    for power in (160, 200):
        best = None

        def rec(i, counts, used, slots):
            nonlocal best
            if i == len(ms):
                fills = [report["machines"][m]["fill"][k] for m, k in counts.items() if k]
                val = sum(x["score"] for x in fills)
                if sum(x["cash"] for x in fills) > budget:
                    return
                if best is None or val > best[0]:
                    best = (val, dict(counts))
                return
            m = ms[i]
            mach = report["machines"][m]
            for k in range(0, mach["max"] + 1):
                if used + k * mach["power"] > power or slots + k > hotspots:
                    break
                if k and mach["fill"][k]["score"] <= mach["fill"].get(k - 1, {"score": 0})["score"]:
                    break  # another copy adds nothing (we can't buy enough inputs)
                counts[m] = k
                rec(i + 1, counts, used + k * mach["power"], slots + k)
            counts[m] = 0
        rec(0, {}, 0, 0)
        ms_ = {m: k for m, k in best[1].items() if k}
        out[power] = {"worst_week": sum(report["machines"][m]["fill"][k]["worst_week"] for m, k in ms_.items()),
                      "machines": ms_,
                      "cash": sum(report["machines"][m]["fill"][k]["cash"] for m, k in ms_.items())}
    cur = {"alch": 2, "disasm": 1}
    out["current"] = {"worst_week": sum(report["machines"][m]["fill"][k]["worst_week"] for m, k in cur.items()),
                      "machines": cur, "cash": sum(report["machines"][m]["fill"][k]["cash"] for m, k in cur.items())}
    return out


# ---------- weekly schedule ----------
DEFAULT_SCHEDULE = {"setup": {"alch": 2, "disasm": 1},  # what's built in the Invention Guild
                    "refill_weekday": 1, "refill_hour": 18,   # Tuesday 18:00 local: inputs go in
                    "buy_days": 7}                            # the week runs reset to reset: buy all week


def next_refill(t: dt.datetime, sch: dict) -> dt.datetime:
    """The next refill time at or after t (local)."""
    base = t.replace(hour=sch["refill_hour"], minute=0, second=0, microsecond=0)
    d = (sch["refill_weekday"] - t.weekday()) % 7
    r = base + dt.timedelta(days=d)
    return r if r > t else r + dt.timedelta(days=7)


def in_buy_window(t: dt.datetime, sch: dict) -> bool:
    return next_refill(t, sch) - t <= dt.timedelta(days=sch["buy_days"])


def shopping_list(rep: dict, setup: dict) -> list[dict]:
    """This week's inputs for the built machines: [{machine, item, qty, buy_at, timing}]."""
    out = []
    for m, k in setup.items():
        if not k or m not in rep["machines"]:
            continue
        for p in rep["machines"][m]["fill"][k]["items"]:
            out.append({"machine": MACHINES[m]["name"], "kind": m, "item": p["item"], "qty": p["qty"],
                        "buy_at": p["buy_at"], "timing": p.get("timing"), "p10": p["p10"]})
    return out


# ---------- text ----------
def gp(n):
    a = abs(n)
    s = f"{a / 1e9:.2f}b" if a >= 1e9 else f"{a / 1e6:.1f}m" if a >= 1e6 else f"{a / 1e3:.0f}k" if a >= 1e3 else f"{a:.0f}"
    return ("-" if n < 0 else "") + s


def render(rep):
    L = [f"# Invention machines — weekly report ({rep['at'][:16].replace('T', ' ')})",
         f"Divine charge {rep['dc_each']:,.0f} · nature rune {rep['nature']:,.0f} · "
         "profits are per machine per week, after tax and running costs. "
         "**Bad week** = 1-in-10 worst of the last 90 days; **typical** = median. "
         f"**We can buy** = our share if {COMPETITORS:,} other players do the same: 1/{COMPETITORS + 1:,} of a slow week's sellers "
         "(25th-percentile day x 7), capped by the 4h buy limit. One input item per machine type per week."]
    names = {"alch": "Alchemiser mk. II", "disasm": "Auto disassembler mk. II → empty divine charges",
             "tanner": "Optimised hide tanner", "plank": "High capacity plank maker",
             "potion": "Partial potion producer DX (volatile — shown for comparison only)"}
    for m, d in rep["machines"].items():
        f1 = d["fill"][1]
        L.append(f"\n## {names[m]} — {d['power']} power, {d['per_week']:,} inputs/week")
        if not d["items"]:
            L.append("No input stayed profitable through the last 30 days.")
            continue
        L.append(f"One machine: bad week **{gp(f1['worst_week'])}**, typical {gp(f1['typical_week'])}, "
                 f"cash to stock it {gp(f1['cash'])} ({f1['worst_week'] / max(f1['cash'], 1) * 100:.1f}% a week in a bad week)"
                 + ("" if f1["filled"] >= f1["need"] else f" (only {f1['filled']:,}/{f1['need']:,} inputs buyable)"))
        L.append("| Input | profit each (now / bad week / typical) | days it lost money (90d) | sellers on a slow day | we can buy per week | usually cheapest |")
        L.append("|---|---|---|---|---|---|")
        def line(r, tag=""):
            t = r["timing"]
            tt = f"{t['day']} ({t['saving'] * 100:+.1f}%, {t['weeks']} weeks)" if t else "no reliable pattern"
            vol = f"{r['vol_day']:,}/day" + (f" (buyers of the output {r['out_vol_day']:,}/day)" if r.get("out_vol_day") is not None else "")
            name = r["item"] + (" → " + r["out"] if r["out"] and m != "disasm" else "")
            return (f"| {tag}{name} | {r['now']:,.0f} / {r['p10']:,.0f} / {r['median']:,.0f} | "
                    f"{round(r['loss_share'] * r['days'])}/{r['days']} | {vol} | {r['per_week_max']:,} | {tt} |")
        for r in d["items"][:6]:
            L.append(line(r))
        for r in d.get("current", []):
            if r not in d["items"][:6]:
                L.append(line(r, "*(you now)* "))
        mix = ", ".join(f"{p['qty']:,} {p['item']}" for p in f1["items"])
        L.append(f"Item for the week (one machine): {mix}")
    L.append("\n## Best machine set-up (bad-week profit, only what volume allows)")
    for k, v in rep["setups"].items():
        label = "Now (2 alchemisers + 1 disassembler)" if k == "current" else f"{k} power"
        L.append(f"- {label}: **{gp(v['worst_week'])}/week** in a bad week, needs {gp(v['cash'])} of stock — "
                 + ", ".join(f"{n}× {MACHINES[m]['name']}" for m, n in v["machines"].items()))
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--update-simple", action="store_true")
    a = ap.parse_args()
    if a.update_simple or not SIMPLE.exists():
        print(f"simple parts table: {update_simple()} items", file=sys.stderr)
    rep = analyse()
    out = HERE.parent / "ledger" / "machines.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print(json.dumps(rep, indent=1) if a.json else render(rep))


if __name__ == "__main__":
    main()
