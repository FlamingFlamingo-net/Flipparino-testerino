#!/usr/bin/env python3
"""RS3 trade desk: twice-daily GE plan + ledger for feedback (stdlib only).

  python tools/desk.py plan [--dry-run] [--review-all]
        build the next plan (max 8 GE slots) from LIVE GE prices, save it, print markdown.
        --review-all re-checks every open offer now instead of waiting until it is stale.
  python tools/desk.py status                 cash, holdings, open offers (numbered as in the latest plan)
  python tools/desk.py fill N [--qty Q | --to T] [--price P] [--close]
        record fills for trade N of the latest plan. Default: the whole remaining qty at the offer price.
        --qty = newly filled amount, --to = total filled so far, --close = offer aborted after this.
  python tools/desk.py cancel N               offer N was aborted / never placed (frees the slot)
  python tools/desk.py set --cash X | --capital X | --tax PCT
  python tools/desk.py holding NAME QTY COST_EACH    add items you already own
  python tools/desk.py watch [add|rm NAME]    the always-analysed watchlist

Plans are assumed to be executed exactly as written; feedback corrects the ledger.
Every write keeps a backup in ledger/backups/.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import shutil
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rsmarket as rm  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "ledger"
STATE = LEDGER / "state.json"
SLOTS = 8
RECIPE_BY_INPUT = {r[0]: r for r in rm.RECIPES}

DEFAULT_CFG = {
    "tax_rate": 0.02, "members": True,
    "capture": 0.10,          # share of each side's daily flow we expect to get filled
    "hold_hours": 12,         # time between check-ins: an offer gets this long to fill
    "dip_z": 1.5,
    "min_price": 100, "min_flow": 500, "candidates": 80,
    "max_item_share": 0.25,   # never put more than 25 % of capital in one item
    "min_conf": 0.5, "min_profit": 50_000, "max_dips": 2,
    "min_roi": 0.005,         # ≥ 0.5 % after tax: thinner margins vanish with any price move
    "min_slot_hours": 6,      # ranking = profit per slot-hour; faster than this doesn't free a slot sooner
    "stale_hours": 10,        # an offer older than this gets re-priced or aborted
    "watchlist": rm.DEFAULT_WATCHLIST,
    "blacklist": [],          # items the user won't trade (e.g. being removed from the game)
    "max_price": 50_000_000,  # user: no high-value items / combat gear unless very safe
}
DEFAULT_STATE = {
    "version": 3, "capital": 100_000_000, "cash": 100_000_000, "cfg": DEFAULT_CFG,
    "orders": [], "holdings": {}, "realized": [], "plans": [], "next_id": 1,
}


# ---------------- state ----------------
def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def load_state() -> dict:
    if not STATE.exists():
        LEDGER.mkdir(parents=True, exist_ok=True)
        return json.loads(json.dumps(DEFAULT_STATE))
    s = json.loads(STATE.read_text(encoding="utf-8"))
    if s.get("version", 1) < 3:  # v3 = live prices: new keys and retuned thresholds
        for k in ("band_k", "min_volume"):
            s["cfg"].pop(k, None)
        for k in ("capture", "min_price", "candidates", "dip_z"):
            s["cfg"][k] = DEFAULT_CFG[k]
        s["version"] = 3
    for k, v in DEFAULT_CFG.items():
        s["cfg"].setdefault(k, v)
    return s


def save_state(s: dict) -> None:
    LEDGER.mkdir(parents=True, exist_ok=True)
    if STATE.exists():
        (LEDGER / "backups").mkdir(exist_ok=True)
        shutil.copy2(STATE, LEDGER / "backups" / f"state-{now():%Y%m%d-%H%M%S-%f}.json")
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1), encoding="utf-8")
    tmp.replace(STATE)


filled = lambda o: sum(f["qty"] for f in o["fills"])  # noqa: E731
remaining = lambda o: o["qty"] - filled(o)  # noqa: E731
gp = lambda n: f"{round(n):,}"  # noqa: E731


def gpc(n: float) -> str:
    n = float(n)
    for div, suf in ((1e9, "b"), (1e6, "m"), (1e3, "k")):
        if abs(n) >= div:
            return f"{n / div:.1f}{suf}".replace(".0", "")
    return str(round(n))


def open_orders(s):
    return [o for o in s["orders"] if o["status"] == "open"]


def reserved_cash(s):
    return sum(remaining(o) * o["price"] for o in open_orders(s) if o["side"] == "buy")


def order_by_number(s, n: int) -> dict:
    if not s["plans"]:
        sys.exit("No plan yet.")
    oid = s["plans"][-1]["map"].get(str(n))
    if not oid:
        sys.exit(f"Trade {n} is not in plan {s['plans'][-1]['id']}.")
    return next(o for o in s["orders"] if o["id"] == oid)


def hours_since(iso: str) -> float:
    return (now() - dt.datetime.fromisoformat(iso)).total_seconds() / 3600


def round_qty(q: int, limit: int, budget: float, price: float, step: int = 100) -> int:
    """User preference: buy quantities in whole hundreds (or a multiple of `step`).
    Nearest step, but never above the 4-hour buy limit or the cash allowed; below one step the
    exact number is kept."""
    if q < step:
        return q
    cap = min(limit if limit > 0 else math.inf, budget // price)
    r = round(q / step) * step
    if r > cap:
        r = q // step * step
    return int(r)


# ---------------- away (multi-day, set-and-forget) ----------------
def away_ideas(s, mk, cfg, per, days, banned, used_items, extra_ids) -> list[dict]:
    """Liquid items whose price, over the last 30 days, kept climbing from today's buy level to a
    profitable sell level within `days`. Buy now, list the sell at that level, leave it."""
    items = mk["items"]

    def turnover(it):  # liquid, affordable in bulk (≥ 20 units with this slot's cash)
        rp = rm.ref_prices(it)
        if (not rp or rp[0] < cfg["min_price"] or rp[0] * 20 > per or it["name"] in banned
                or (not cfg["members"] and it["members"]) or min(it["lv24"], it["hv24"]) < 2000):
            return 0
        return min(it["lv24"], it["hv24"]) * rp[0]
    pool = sorted(items.values(), key=turnover, reverse=True)[:120]
    ids = ({it["id"] for it in pool if turnover(it) > 0} | {i for i in extra_ids if i in items}) - used_items
    series = rm.load_series30(ids)
    out = []
    for i in ids:
        it = items[i]
        if it["name"] in banned:
            continue
        rp = rm.ref_prices(it)
        if not rp:
            continue
        # bid at today's instant-sell price so the buy fills soon and the sell can be listed before leaving
        buy = max(rp[0], (it["low"] or 0) + 1)
        a = rm.away_analysis(series.get(i), buy, cfg["tax_rate"], it["name"], days)
        if not a or a["net"] / buy < 0.02 or a["worst_dip"] > 0.10 or a["trend30"] < -0.10:
            continue
        limit = it["limit"] or math.inf
        qty = math.floor(min(limit * 6 * days, cfg["capture"] * it["lv24"] * days,
                             cfg["capture"] * it["hv24"] * days, per / buy))
        if qty < 1:
            continue
        flags = []
        if a["worst_dip"] > 0.05: flags.append(f"can dip {a['worst_dip']:.0%} while waiting")
        if a["pct_rank"] > 0.7: flags.append("already high in its 30-day range")
        if it["limit"] and qty > it["limit"]: flags.append(f"buys over {math.ceil(qty / it['limit'] / 6 * 24)}h (4h limit)")
        conf = 1.0
        for f in flags:
            conf *= 0.6 if not f.startswith("buys over") else 1.0
        total = a["net"] * qty
        out.append({"strategy": "away", "item": it, "buy": buy, "sell": a["target"], "profit": a["net"], "qty": qty,
                    "total": total, "roi": a["net"] / buy, "flags": flags, "confidence": conf, "hit": a["hit"],
                    "worst_dip": a["worst_dip"], "days": days, "watch": i in extra_ids, "hours": days * 24,
                    "score": total * a["hit"] * conf})
    return out


# ---------------- 4-hour buy limits ----------------
def limit_state(s, it) -> tuple[float, float, str | None]:
    """(units left in the current 4h window, hours until it resets, reset clock text).
    The window starts at the first buy fill and lasts 4h; a user-given reset time overrides it."""
    limit = it.get("limit") or 0
    if not limit:
        return math.inf, 0.0, None
    key = str(it["id"])
    reset_iso = s.get("limit_resets", {}).get(key)
    t = now()
    if not reset_iso or dt.datetime.fromisoformat(reset_iso) <= t:
        return limit, 0.0, None
    reset = dt.datetime.fromisoformat(reset_iso)
    start = reset - dt.timedelta(hours=4)
    used = sum(f["qty"] for o in s["orders"] if o["side"] == "buy" and o["item_id"] == it["id"]
               for f in o["fills"] if dt.datetime.fromisoformat(f["at"]) >= start)
    if s.get("limit_used", {}).get(key) is not None:
        used = max(used, s["limit_used"][key])
    return max(0, limit - used), (reset - t).total_seconds() / 3600, f"{reset:%H:%M}"


# ---------------- track record ----------------
HALF_LIFE_DAYS = 14


def track_record(s) -> dict:
    """Per item, from the ledger: time-decayed wins/losses of finished sales and buy offers that
    were aborted without a single fill. Returns {name: {...}} with a ranking multiplier."""
    rec: dict = {}

    def w(iso):
        return 0.5 ** (hours_since(iso) / 24 / HALF_LIFE_DAYS)

    def get(name):
        return rec.setdefault(name, {"wins": 0.0, "losses": 0.0, "dead": 0.0, "profit": 0, "n_win": 0, "n_loss": 0, "n_dead": 0})
    sales: dict = {}
    for r_ in s["realized"]:  # merge partial fills of one sale into one outcome per day
        key = (r_["name"], r_["at"][:10])
        x = sales.setdefault(key, {"profit": 0, "cost": 0, "at": r_["at"]})
        x["profit"] += r_["profit"]
        x["cost"] += r_["buy_each"] * r_["qty"]
    for (name, _), x in sales.items():
        g = get(name)
        g["profit"] += x["profit"]
        if x["profit"] > 0:
            # a win counts fully from 1 % return on cost; a 0.1 % scrape barely counts
            g["wins"] += w(x["at"]) * min(1.0, x["profit"] / max(1, x["cost"]) / 0.01); g["n_win"] += 1
        else:
            g["losses"] += w(x["at"]); g["n_loss"] += 1
    for o in s["orders"]:
        if o["side"] == "buy" and o["status"] == "cancelled" and filled(o) == 0 and any(
                e.get("action") == "abort" and "no fills" in (e.get("note") or "") for e in o.get("log", [])):
            g = get(o["name"]); g["dead"] += w(o.get("closed") or o["placed"]); g["n_dead"] += 1
    for g in rec.values():
        g["mult"] = min(1.75, max(0.5, 1 + 0.25 * g["wins"] - 0.35 * g["losses"] - 0.1 * g["dead"]))
        parts = []
        if g["n_win"]: parts.append(f"{g['n_win']} win{'s' * (g['n_win'] > 1)}")
        if g["n_loss"]: parts.append(f"{g['n_loss']} loss{'es' * (g['n_loss'] > 1)}")
        if g["n_dead"]: parts.append(f"{g['n_dead']} unfilled")
        g["label"] = f"↻ {', '.join(parts)}" + (f", {'+' if g['profit'] >= 0 else ''}{gpc(g['profit'])}" if g["n_win"] + g["n_loss"] else "")
    return rec


# ---------------- planning ----------------
def sell_target(s, it, hold, planned=None, st=None):
    """Price for selling a holding: profit first, then break-even, then cut the loss."""
    rate = s["cfg"]["tax_rate"]
    cost_each = hold["cost"] / hold["qty"]
    be = rm.break_even_sell(cost_each, rate, it["name"])
    rp = rm.ref_prices(it, st)
    base = rp[1] if rp else (planned or be)
    days = hours_since(hold["since"]) / 24
    patience = {"dip": 5, "away": hold.get("hold_days", 3) + 1}.get(hold.get("strategy"), 2)
    if days < patience:
        # dips aim for the planned recovery price; flips follow the live market
        return max(planned or 0 if hold.get("strategy") in ("dip", "away") else 0, base, be), None
    if days < patience * 2:
        return max(base, be), f"held {days:.0f}d: target lowered to the market"
    if base < be:
        return base, f"held {days:.0f}d: cutting the loss"
    return base, None


def cmd_plan(args):
    s = load_state()
    cfg = s["cfg"]
    mk = rm.load_market()
    items = mk["items"]
    t = now()
    watch_ids = {mk["by_name"][n]["id"] for n in cfg["watchlist"] if n in mk["by_name"]}

    open_o = open_orders(s)
    used_items = {o["item_id"] for o in open_o} | {int(k) for k, h in s["holdings"].items() if h["qty"] > 0}
    available = s["cash"] - reserved_cash(s)
    budget_each = min(s["capital"] * cfg["max_item_share"], max(available, 0) / SLOTS * 2)

    # pre-rank on bulk data only, then fetch 7-day hourly history for the best + watchlist
    def potential(it):
        rp = rm.ref_prices(it)
        if not rp or rp[0] < cfg["min_price"] or rp[0] > cfg["max_price"] or (not cfg["members"] and it["members"]):
            return 0
        flow = min(it["lv24"], it["hv24"])
        if flow < cfg["min_flow"]:
            return 0
        profit = rm.net_sell(rp[1], cfg["tax_rate"], it["name"]) - rp[0]
        q = min(it["limit"] or math.inf, budget_each / rp[0], cfg["capture"] * flow * cfg["hold_hours"] / 24)
        return profit * q if profit > 0 and q >= 1 else 0
    banned = set(cfg.get("blacklist", [])) | {"Bond"}
    ranked = sorted((it for it in items.values() if it["name"] not in banned), key=potential, reverse=True)
    cand_ids = {it["id"] for it in ranked[: cfg["candidates"]] if potential(it) > 0}
    record = track_record(s)
    proven_ids = {mk["by_name"][n]["id"] for n, g in record.items() if g["n_win"] and n in mk["by_name"]}
    want = cand_ids | watch_ids | proven_ids | used_items | {
        h["process"]["into_id"] for h in s["holdings"].values() if h["qty"] > 0 and h.get("process")}
    series = rm.load_series(want)
    failed = sorted(i for i, p in series.items() if p is None)
    if len(failed) > 0.2 * len(series):
        sys.exit(f"Price history API failed for {len(failed)}/{len(series)} items; no plan saved. Try again later.")
    stats = {i: rm.series_stats(p, cfg["tax_rate"], items[i]["name"]) for i, p in series.items() if i in items}
    STATS.update(stats)

    actions = []  # (kind, order, note)

    def best_for(it, budget, strategy=None):
        if it["name"] in RECIPE_BY_INPUT and strategy in (None, "process"):
            x = rm.suggest_process(RECIPE_BY_INPUT[it["name"]], mk, stats, cfg, budget)
            if x and x["confidence"] >= cfg["min_conf"]:
                return x
        xs = [x for x in rm.suggest(it, stats.get(it["id"]), cfg, budget, it["id"] in watch_ids)
              if x["confidence"] >= cfg["min_conf"] and strategy in (None, x["strategy"])]
        return max(xs, key=lambda x: x["score"]) if xs else None

    # 1) existing offers: keep, re-price, or abort
    for o in open_o:
        it = items.get(o["item_id"])
        stale = args.review_all or hours_since(o["placed"]) >= cfg["stale_hours"]
        if not it or not stale or o.get("pinned"):
            actions.append(("keep", o, None))
            continue
        if o["side"] == "buy":
            budget = remaining(o) * o["price"] * 1.3
            x = best_for(it, budget, o.get("strategy")) or best_for(it, budget)
            if not x:
                actions.append(("abort", o, "no longer profitable at live prices"))
            elif o.get("tightened") and filled(o) == 0 and not args.review_all:
                actions.append(("abort", o, "still no fills after bidding higher"))
            elif (abs(x["buy"] - o["price"]) / o["price"] > 0.005
                  or (o["price"] > x["buy"] and o["price"] - x["buy"] > 0.5 * x["profit"])):
                actions.append(("reprice", o, (x["buy"], x["sell"], None, x)))
            elif filled(o) == 0 and not args.review_all:
                # nothing bought in a whole session at this price: bid a quarter into the margin, once
                tighter = x["buy"] + max(1, (x["sell"] - x["buy"]) // 4)
                per_unit = x["profit_out"] / x["ratio"] if x["strategy"] == "process" else x["profit"]
                if per_unit - (tighter - x["buy"]) > 0:
                    if not args.dry_run:
                        o["tightened"] = True
                    actions.append(("reprice", o, (tighter, x["sell"], "no fills yet: bidding higher", x)))
                else:
                    actions.append(("abort", o, "no fills and no room to bid higher"))
            else:
                actions.append(("keep", o, None))
        else:
            hold = s["holdings"].get(str(o["item_id"]))
            if not hold or hold["qty"] <= 0:
                actions.append(("keep", o, None))
                continue
            price, note = sell_target(s, it, hold, o.get("target_sell"), stats.get(it["id"]))
            if abs(price - o["price"]) / o["price"] > 0.005:
                actions.append(("reprice", o, (price, None, note, None)))
            else:
                actions.append(("keep", o, None))

    slots_used = sum(1 for k, *_ in actions if k != "abort")
    live = lambda o: not any(a[0] == "abort" and a[1] is o for a in actions)  # noqa: E731

    # 2) holdings not yet on sale -> new sell offers (sells get slots before buys)
    for key, hold in list(s["holdings"].items()):
        if hold["qty"] <= 0 or slots_used >= SLOTS:
            continue
        iid = int(key)
        if any(o["side"] == "buy" and o["item_id"] == iid and live(o) for o in open_o):
            continue  # sell once the buy offer is finished or aborted; never trade against yourself
        on_sale = sum(remaining(o) for o in open_o if o["side"] == "sell" and o["item_id"] == iid and live(o))
        qty = hold["qty"] - on_sale
        if qty <= 0:
            continue
        note = None
        proc = hold.get("process")
        if proc:  # grind first: convert inputs into the output holding
            n_out = qty // proc["ratio"]
            if n_out < 1:
                continue
            used = n_out * proc["ratio"]
            cost = hold["cost"] / hold["qty"] * used
            hold["qty"] -= used
            hold["cost"] -= cost
            out = s["holdings"].setdefault(str(proc["into_id"]), {"name": proc["into"], "qty": 0, "cost": 0})
            if out["qty"] <= 0:
                out.update(since=hold["since"], strategy="process", qty=0, cost=0)
            out["qty"] += n_out
            out["cost"] += cost
            out["target_sell"] = hold.get("target_sell")
            note = f"first grind {gp(used)} {hold['name']} into {gp(n_out)} {proc['into']} (pestle and mortar)"
            iid, hold, qty = proc["into_id"], out, n_out
        it = items.get(iid)
        if not it:
            continue
        price, why = sell_target(s, it, hold, hold.get("target_sell"), stats.get(iid))
        note = "; ".join(x for x in (note, why) if x) or None
        actions.append(("new", new_order(s, "sell", it, qty, price, hold.get("strategy", "spread")), note))
        slots_used += 1

    # 3) free slots -> new buys
    available += sum(remaining(a[1]) * a[1]["price"] for a in actions if a[0] == "abort" and a[1]["side"] == "buy")
    available -= sum((a[2][0] - a[1]["price"]) * remaining(a[1]) for a in actions
                     if a[0] == "reprice" and a[1]["side"] == "buy")
    free = SLOTS - slots_used
    if free > 0 and available > 0:
        per = min(s["capital"] * cfg["max_item_share"], available / free)
        ideas = []
        for i in (cand_ids | watch_ids | proven_ids) - used_items:
            it = items[i]
            if it["name"] in banned or (rm.ref_prices(it) or (0,))[0] > cfg["max_price"]:
                continue
            if it["name"] in RECIPE_BY_INPUT:
                x = rm.suggest_process(RECIPE_BY_INPUT[it["name"]], mk, stats, cfg, per)
                if x: ideas.append(x)
            ideas += rm.suggest(it, stats.get(i), cfg, per, i in watch_ids)
        if args.away:  # away mode: only set-and-forget trades for the free slots
            ideas = away_ideas(s, mk, cfg, per, args.away, banned, used_items, watch_ids | proven_ids)
        ideas = [x for x in ideas if x["confidence"] >= cfg["min_conf"] and x["total"] >= cfg["min_profit"]]
        for x in ideas:  # 4-hour buy limit already partly used?
            left, wait, clock = limit_state(s, x["item"])
            units = x["qty"]
            if left >= units or (x["strategy"] == "away" and clock is None):
                continue  # away buys spread over several limit windows by design
            if left >= max(1, units * 0.25):
                k = left / units
                x["qty"] = int(left) if x["strategy"] != "process" else int(left // x["ratio"] * x["ratio"])
                x["total"] *= k
                x["score"] *= k
                x["flags"] = x["flags"] + [f"limit: {int(left):,} left until {clock}"]
            else:  # slot would sit idle until the window resets
                h = x.get("hours") or 6
                x["score"] *= max(h, 6) / max(h + wait, 6)
                x["flags"] = x["flags"] + [f"buy limit resets {clock}, fills after that"]
        for x in ideas:  # learn from our own results: proven items up, failures down
            g = record.get(x["item"]["name"])
            if g:
                x["score"] *= g["mult"]
                x["record"] = g["label"]
        ideas.sort(key=lambda x: x["score"], reverse=True)
        picked, dips, seen = [], 0, set()
        for x in ideas:
            if len(picked) >= free:
                break
            iid = x["item"]["id"]
            out_id = x["out"]["id"] if x["strategy"] == "process" else None
            if iid in seen or out_id in seen or (x["strategy"] == "dip" and dips >= cfg["max_dips"]):
                continue
            if x["strategy"] == "dip" and {"falling", "still dropping"} & set(x["flags"]):
                continue  # don't catch falling knives
            seen |= {iid, out_id} - {None}
            dips += x["strategy"] == "dip"
            picked.append(x)
        for x in picked:
            it = x["item"]
            if x["strategy"] == "process":
                step = math.lcm(100, x["ratio"])
                qty = round_qty(x["qty"], it["limit"], per, x["buy"], step if x["qty"] >= step else x["ratio"])
                est = x["profit_out"] * (qty // x["ratio"])
            else:
                qty = round_qty(x["qty"], it["limit"], per, x["buy"])
                est = x["profit"] * qty
            if qty < 1 or est < cfg["min_profit"]:
                continue
            o = new_order(s, "buy", it, qty, x["buy"], x["strategy"])
            o.update(target_sell=x["sell"], est_profit=est, flags=x["flags"], watch=x["watch"],
                     est_hours=round(x.get("hours", 0), 1))
            if x["strategy"] == "away":
                o.update(hold_days=x["days"], hit=round(x["hit"], 2), worst_dip=round(x["worst_dip"], 3),
                         est_profit=round(x["profit"] * qty * x["hit"]))
            if x.get("record"):
                o["record"] = x["record"]
            if x["strategy"] == "process":
                o["process"] = {"into_id": x["out"]["id"], "into": x["out"]["name"], "ratio": x["ratio"]}
            actions.append(("new", o, None))

    # 4) number and record
    assign_slots(s, actions)
    rank = {"abort": 0, "reprice": 1, "new": 2, "keep": 3}
    # walk the GE screen slot by slot; inside a slot, abort before the replacement
    actions.sort(key=lambda a: (a[1].get("slot") or 99, rank[a[0]]))
    # unique per run: several plans can happen in one half-day (refresh), and widget rows are
    # rebuilt by matching order logs to this id
    plan_id = f"{t:%Y-%m-%d}-{'am' if t.hour < 12 else 'pm'}-{t:%H%M%S}"
    taken = {p["id"] for p in s["plans"]}
    base_id, k = plan_id, 2
    while plan_id in taken:
        plan_id, k = f"{base_id}-{k}", k + 1
    pmap, lines = {}, []
    for n, (kind, o, note) in enumerate(actions, 1):
        pmap[str(n)] = o["id"]
        line = render_action(n, kind, o, note, items, s)
        if o.get("slot"):
            line = line.replace(f"{n}) ", f"{n}) **[Slot {o['slot']}]** ", 1)
        lines.append(line)

    if not args.dry_run:
        for kind, o, note in actions:
            if kind == "abort":
                o["status"] = "cancelled"
                o["closed"] = t.isoformat()
                o.setdefault("log", []).append({"at": t.isoformat(), "action": "abort", "plan": plan_id, "note": note})
            elif kind == "reprice":
                o["price"] = note[0]
                o["placed"] = t.isoformat()
                o.setdefault("log", []).append({"at": t.isoformat(), "action": "reprice", "price": note[0], "plan": plan_id,
                                                "note": note[2], "then_sell": note[1]})
                x = note[3]
                if o["side"] == "buy" and x:
                    o["target_sell"] = note[1]
                    if x["strategy"] != o.get("strategy"):
                        o["strategy"] = x["strategy"]
                        o.pop("process", None)
                        if x["strategy"] == "process":
                            o["process"] = {"into_id": x["out"]["id"], "into": x["out"]["name"], "ratio": x["ratio"]}
            elif kind == "new":
                o["plan"] = plan_id
                if note:
                    o["note"] = note
                s["orders"].append(o)
        s["plans"].append({"id": plan_id, "at": t.isoformat(), "prices_at": mk["fetched"], "map": pmap})
        save_state(s)

    md = render_plan(s, t, mk, lines, args.dry_run, len(failed))
    if not args.dry_run:
        (LEDGER / "plans").mkdir(parents=True, exist_ok=True)
        html = write_widget(s, s["plans"][-1])
        md += f'\n\n::preview{{file="{html.as_posix()}"}}'
        (LEDGER / "plans" / f"{plan_id}.md").write_text(md, encoding="utf-8")
    print(md)


# ---------------- copy-button widget ----------------
def plan_rows(s, plan) -> list[dict]:
    """Rebuild a plan's rows from the ledger (works for any saved plan)."""
    rows = []
    by_id = {o["id"]: o for o in s["orders"]}
    for n, oid in sorted(plan["map"].items(), key=lambda kv: int(kv[0])):
        o = by_id[oid]
        log = next((e for e in reversed(o.get("log", [])) if e.get("plan") == plan["id"]), None)
        rem = o["qty"] - sum(f["qty"] for f in o["fills"] if f["at"] < plan["at"])
        row = {"n": int(n), "item": o["name"], "star": bool(o.get("watch")), "slot": o.get("slot")}
        if log and log["action"] == "abort":
            row.update(verb="ABORT", qty=rem, price=o["price"],
                       note=f"abort this {o['side']} offer and collect what filled" + (f": {log['note']}" if log.get("note") else ""))
        elif log and log["action"] == "reprice":
            row.update(verb="CHANGE", qty=rem, price=log["price"], note=f"{o['side']} offer: abort and re-place at the new price"
                       + (f"; {log['note']}" if log.get("note") else ""))
            if o["side"] == "buy" and (log.get("then_sell") or o.get("target_sell")):
                row["then_sell"] = log.get("then_sell") or o.get("target_sell")
        elif o.get("plan") == plan["id"]:
            row.update(verb=o["side"].upper(), qty=o["qty"], price=o["price"])
            if o["side"] == "buy":
                row["then_sell"] = o.get("target_sell")
                row["trait"] = trait(o)
                if o.get("process"):
                    row["grind"] = {"into": o["process"]["into"], "n": o["qty"] // o["process"]["ratio"]}
            note = o.get("note") or ""
            if note.startswith("first grind"):
                row["pre"] = note.split(";")[0].capitalize()
                note = note.split("; ", 1)[1] if "; " in note else ""
            if o.get("record"):
                note = f"{note} · {o['record']}" if note else o["record"]
            if note:
                row["note"] = note
        else:
            row.update(verb="KEEP", qty=rem, price=o["price"], note=f"{o['side']} offer still in the GE, leave it")
            if o["side"] == "buy" and o.get("target_sell") and not o.get("process"):
                row["then_sell"] = o["target_sell"]
        rows.append(row)
    return rows


def write_widget(s, plan) -> Path:
    t = dt.datetime.fromisoformat(plan["at"])
    data = {
        "title": f"{t:%A %d %b} — {'morning' if t.hour < 12 else 'afternoon' if t.hour < 17 else 'evening'} plan",
        "rows": plan_rows(s, plan),
        "footer": "Copy puts the exact item name on your clipboard for the GE search. Prices can't be pasted: "
                  "type them, then check the GE total matches the line under the offer before confirming.",
    }
    tpl = (Path(__file__).with_name("plan_widget.html")).read_text(encoding="utf-8")
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    out = (LEDGER / "plans" / f"{plan['id']}.html").resolve()
    out.write_text(tpl.replace("/*DATA*/null", blob), encoding="utf-8")
    return out


def cmd_widget(_a):
    s = load_state()
    if not s["plans"]:
        sys.exit("No plan yet.")
    print(f'::preview{{file="{write_widget(s, s["plans"][-1]).as_posix()}"}}')


def free_slots(s, exclude=()) -> list[int]:
    taken = {o.get("slot") for o in open_orders(s) if o["id"] not in exclude}
    return [k for k in range(1, SLOTS + 1) if k not in taken]


def assign_slots(s, actions):
    """Give every live offer a GE slot (1-8). Kept/changed offers stay where they are; an aborted
    offer frees its slot; new offers take a slot just freed for the same item, else the lowest free."""
    live = [a for a in actions if a[0] != "abort"]
    taken = {a[1]["slot"] for a in live if a[1].get("slot")}
    freed_by_item = {a[1]["item_id"]: a[1]["slot"] for a in actions if a[0] == "abort" and a[1].get("slot")}
    for kind, o, _ in live:
        if o.get("slot") and o["slot"] in taken and kind != "new":
            continue
        want = freed_by_item.get(o["item_id"])
        if want and want not in taken:
            o["slot"] = want
        else:
            free = [k for k in range(1, SLOTS + 1) if k not in taken]
            o["slot"] = free[0] if free else None
        if o["slot"]:
            taken.add(o["slot"])


def trait(o) -> str:
    """Speed label shown on every buy: ⚡ fast (≤ 8 h in a slot), 🕒 slow, 🧳 away (multi-day)."""
    if o.get("strategy") == "away":
        return f"🧳 away {o.get('hold_days', 3):g}d"
    if o.get("strategy") == "dip":
        return "🕒 slow (dip, days)"
    h = o.get("est_hours") or 0
    hrs = f" ~{max(1, round(h))}h" if h else ""
    return f"⚡ fast{hrs}" if h and h <= 8 else f"🕒 slow{hrs}"


def new_order(s, side, it, qty, price, strategy):
    oid = f"T{s['next_id']}"
    s["next_id"] += 1
    return {"id": oid, "side": side, "item_id": it["id"], "name": it["name"], "qty": int(qty),
            "price": int(price), "fills": [], "status": "open", "placed": now().isoformat(),
            "strategy": strategy}


STATS: dict = {}


def market(items, iid):
    it = items.get(iid)
    rp = rm.ref_prices(it, STATS.get(iid)) if it else None
    return f" · market {gp(rp[0] - 1)}–{gp(rp[1] + 1)}" if rp else ""


def render_action(n, kind, o, note, items, s):
    rem = remaining(o)
    side = o["side"].upper()
    star = "★ " if o.get("watch") else ""
    mkt = market(items, o["item_id"])
    if kind == "abort":
        return (f"{n}) **ABORT** {o['side']} offer {o['name']} @ {gp(o['price'])} ({gp(rem)} left){mkt}. "
                f"Collect whatever filled. *{note}*")
    if kind == "reprice":
        new_price, _sell, why, x = note
        extra = f" · then sell @ {gp(x['sell'])}" if x else ""
        return (f"{n}) **CHANGE** {side} {o['name']}: abort and re-place at **{gp(new_price)}** "
                f"for the remaining **{gp(rem)}** (was {gp(o['price'])}){mkt}{extra}" + (f". *{why}*" if why else ""))
    if kind == "new" and o["side"] == "buy":
        flags = f" · ⚑ {', '.join(o['flags'])}" if o.get("flags") else ""
        flags += f" · {o['record']}" if o.get("record") else ""
        head = f"{n}) **BUY** {star}{o['name']} — **{gp(o['qty'])} @ {gp(o['price'])}** (GE total {gp(o['qty'] * o['price'])}){mkt}"
        if o["strategy"] == "process":
            p = o["process"]
            return (f"{head} → grind into **{gp(o['qty'] // p['ratio'])} {p['into']}** → sell @ {gp(o['target_sell'])}"
                    f"{market(items, p['into_id'])} · est. +{gpc(o['est_profit'])}{flags}")
        kind_txt = trait(o)
        if o["strategy"] == "away":
            kind_txt += (f" · hit that price within {o['hold_days']:g}d in {o['hit']:.0%} of past cases"
                         f" · worst dip −{o['worst_dip']:.0%}")
        return f"{head} · then sell @ {gp(o['target_sell'])} · {kind_txt} · est. +{gpc(o['est_profit'])}{flags}"
    if kind == "new":
        hold = s["holdings"].get(str(o["item_id"]), {})
        cost_each = hold.get("cost", 0) / hold["qty"] if hold.get("qty") else 0
        tax = rm.ge_tax(o["price"], s["cfg"]["tax_rate"], o["name"])
        est = (o["price"] - tax - cost_each) * o["qty"]
        pre = f"*{note.split(';')[0]}*, then " if note and note.startswith("first grind") else ""
        tail = note.split("; ", 1)[1] if note and note.startswith("first grind") and "; " in note else (
            None if note and note.startswith("first grind") else note)
        return (f"{n}) {pre}**SELL** {o['name']} — **{gp(o['qty'])} @ {gp(o['price'])}** (GE total {gp(o['qty'] * o['price'])}){mkt} · "
                f"paid {gp(cost_each)} ea · est. {'+' if est >= 0 else ''}{gpc(est)} after tax"
                + (f". *{tail}*" if tail else ""))
    then = (f" · **when bought, sell @ {gp(o['target_sell'])}**" if o["side"] == "buy" and o.get("target_sell")
            and not o.get("process") else "")
    return (f"{n}) keep: {side} {o['name']} {gp(rem)}/{gp(o['qty'])} left @ {gp(o['price'])}{mkt}{then} "
            f"(placed {hours_since(o['placed']):.0f}h ago)")


def render_plan(s, t, mk, lines, dry, failed=0):
    fetched = dt.datetime.fromtimestamp(mk["fetched"]).astimezone()
    real = sum(r["profit"] for r in s["realized"])
    held_cost = sum(h["cost"] for h in s["holdings"].values() if h["qty"] > 0)
    slots = len([ln for ln in lines if "**ABORT**" not in ln])
    out = [
        f"## {t:%A %d %b} — {'morning' if t.hour < 12 else 'afternoon' if t.hour < 17 else 'evening'} plan" + (" (DRY RUN)" if dry else ""),
        f"Live GE prices as of {fetched:%H:%M} · slots after this plan: {slots}/{SLOTS} · ★ = your watchlist",
        "",
        "**Do these trades:**" if lines else "**Nothing to do this round.** No idea cleared the bar.",
        *lines,
        "",
        f"Cash {gpc(s['cash'])} (tied in buy offers {gpc(reserved_cash(s))}) · items held at cost {gpc(held_cost)} · "
        f"realised P/L {'+' if real >= 0 else ''}{gpc(real)} · capital cap {gpc(s['capital'])}",
        *([f"_Note: price history unavailable for {failed} items this run; they were skipped._"] if failed else []),
        "",
        "_Reply with feedback per number, e.g. “1 done, 2 bought 3,000 @ 2,065, 3 never placed, "
        "4 sold 50% @ 199k”. Anything you don't mention is assumed still sitting in the GE._",
    ]
    return "\n".join(out)


# ---------------- feedback ----------------
def apply_fill(s, o, q, price):
    rate = s["cfg"]["tax_rate"]
    key = str(o["item_id"])
    t = now().isoformat()
    o["fills"].append({"qty": q, "price": price, "at": t})
    if o["side"] == "buy":
        lr = s.setdefault("limit_resets", {})
        cur = lr.get(key)
        if not cur or dt.datetime.fromisoformat(cur) <= now():  # first fill opens a new 4h window
            # anchor on the fill's own timestamp so this fill counts inside its window
            lr[key] = (dt.datetime.fromisoformat(t) + dt.timedelta(hours=4)).isoformat()
            s.get("limit_used", {}).pop(key, None)
        s["cash"] -= q * price
        h = s["holdings"].setdefault(key, {"name": o["name"], "qty": 0, "cost": 0})
        if h["qty"] <= 0:
            h.update(since=t, strategy=o.get("strategy", "spread"), cost=0, qty=0)
        h["qty"] += q
        h["cost"] += q * price
        h["target_sell"] = o.get("target_sell")
        if o.get("hold_days"):
            h["hold_days"] = o["hold_days"]
        if o.get("process"):
            h["process"] = o["process"]
    else:
        h = s["holdings"].get(key)
        if not h or h["qty"] < q:
            sys.exit(f"Ledger only holds {h['qty'] if h else 0} {o['name']}; can't sell {q}.")
        tax = rm.ge_tax(price, rate, o["name"]) * q
        cost_each = h["cost"] / h["qty"]
        s["cash"] += q * price - tax
        h["qty"] -= q
        h["cost"] = h["cost"] - cost_each * q if h["qty"] else 0
        s["realized"].append({"at": t, "name": o["name"], "item_id": o["item_id"], "order": o["id"], "qty": q, "buy_each": round(cost_each, 2),
                              "sell_each": price, "tax": tax, "profit": round(q * price - tax - cost_each * q)})
    if remaining(o) <= 0:
        o["status"] = "done"
        o["closed"] = t


def cmd_fill(a):
    s = load_state()
    o = order_by_number(s, a.n)
    if a.to is not None:
        q = a.to - filled(o)
    else:
        q = a.qty if a.qty is not None else remaining(o)
    if q < 0 or q > remaining(o):
        sys.exit(f"Bad quantity {q}: {gp(filled(o))}/{gp(o['qty'])} already filled.")
    if q:
        apply_fill(s, o, q, a.price if a.price is not None else o["price"])
    if a.close and o["status"] == "open":
        o["status"] = "cancelled"
        o["closed"] = now().isoformat()
    msg = ""
    # The plan's "then sell @" is the agreed next step: once a buy is finished, the user lists it
    # right away, so record that sell offer now (unless --no-sell or it's a grind-first order).
    if (o["side"] == "buy" and o["status"] != "open" and o.get("target_sell") and not o.get("process")
            and not a.no_sell and filled(o) > 0):
        sell = new_order(s, "sell", {"id": o["item_id"], "name": o["name"]}, filled(o), o["target_sell"],
                         o.get("strategy", "spread"))
        sell.update(plan=s["plans"][-1]["id"], note="listed at the plan's sell price", watch=o.get("watch", False),
                    slot=o.get("slot"))
        s["orders"].append(sell)
        m = s["plans"][-1]["map"]
        n = max(map(int, m)) + 1
        m[str(n)] = sell["id"]
        msg = (f" Sell offer recorded as trade {n}: {gp(filled(o))} @ {gp(o['target_sell'])}"
               + (f" (slot {sell['slot']})." if sell.get("slot") else "."))
    save_state(s)
    print(f"{o['id']} {o['side']} {o['name']}: +{gp(q)} @ {gp(a.price or o['price'])} → "
          f"{gp(filled(o))}/{gp(o['qty'])} filled, status {o['status']}. Cash {gp(s['cash'])}.{msg}")


def cmd_edit(a):
    """The offer sits in the GE at a different price/qty than the ledger says (e.g. a CHANGE not done)."""
    s = load_state()
    o = order_by_number(s, a.n)
    if a.price is not None:
        o["price"] = a.price
    if getattr(a, "slot", None):
        o["slot"] = a.slot
    if a.pin:
        o["pinned"] = True  # user's decision: plans keep this offer as-is, never re-price it
    if a.qty is not None:
        if a.qty < filled(o):
            sys.exit(f"{gp(filled(o))} already filled; qty can't go below that.")
        o["qty"] = a.qty
    o.setdefault("log", []).append({"at": now().isoformat(), "action": "edit", "price": o["price"], "qty": o["qty"]})
    save_state(s)
    print(f"{o['id']} {o['side']} {o['name']}: now {gp(o['qty'])} @ {gp(o['price'])} ({gp(filled(o))} filled).")


def cmd_cancel(a):
    s = load_state()
    o = order_by_number(s, a.n)
    o["status"] = "cancelled"
    o["closed"] = now().isoformat()
    save_state(s)
    print(f"{o['id']} {o['side']} {o['name']} cancelled ({gp(filled(o))}/{gp(o['qty'])} had filled).")


def cmd_set(a):
    s = load_state()
    if a.cash is not None: s["cash"] = a.cash
    if a.capital is not None: s["capital"] = a.capital
    if a.tax is not None: s["cfg"]["tax_rate"] = a.tax / 100
    save_state(s)
    print(f"cash {gp(s['cash'])} · capital {gp(s['capital'])} · tax {s['cfg']['tax_rate']:.1%}")


def _item_ids():
    return {m["name"]: m["id"] for m in rm.get_json(f"{rm.RT}/mapping")}


def cmd_holding(a):
    s = load_state()
    ids = _item_ids()
    if a.name not in ids:
        sys.exit(f"Unknown GE item name: {a.name!r} (case-sensitive, as on the wiki)")
    h = s["holdings"].setdefault(str(ids[a.name]), {"name": a.name, "qty": 0, "cost": 0})
    if h["qty"] <= 0:
        h.update(since=now().isoformat(), strategy="spread")
    h["qty"] += a.qty
    h["cost"] += a.qty * a.cost_each
    if a.name in RECIPE_BY_INPUT:
        r = RECIPE_BY_INPUT[a.name]
        h["process"] = {"into_id": ids[r[2]], "into": r[2], "ratio": r[1]}
    save_state(s)
    print(f"Holding {a.name}: {gp(h['qty'])} at {gp(h['cost'] / h['qty'])} ea")


def cmd_add(a):
    """Record an offer the user placed outside a plan; it gets the next number in the latest plan."""
    s = load_state()
    ids = _item_ids()
    if a.name not in ids:
        sys.exit(f"Unknown GE item name: {a.name!r} (case-sensitive, as on the wiki)")
    iid = ids[a.name]
    h = None
    if a.side == "sell":
        h = s["holdings"].get(str(iid))
        on_sale = sum(remaining(o) for o in open_orders(s) if o["side"] == "sell" and o["item_id"] == iid)
        if not h or h["qty"] - on_sale < a.qty:
            sys.exit(f"Ledger holds {h['qty'] if h else 0} {a.name} ({on_sale} already on sale); can't list {a.qty}.")
    o = new_order(s, a.side, {"id": iid, "name": a.name}, a.qty, a.price, (h or {}).get("strategy", "spread") if a.side == "sell" else "spread")
    if a.side == "buy":
        o["target_sell"] = None
    o["plan"] = s["plans"][-1]["id"] if s["plans"] else "manual"
    o["note"] = "placed by you"
    o["slot"] = a.slot or (free_slots(s) or [None])[0]
    s["orders"].append(o)
    if s["plans"]:
        m = s["plans"][-1]["map"]
        n = max(map(int, m)) + 1 if m else 1
        m[str(n)] = o["id"]
    save_state(s)
    print(f"Added trade {n if s['plans'] else '?'}: {o['id']} {a.side} {gp(a.qty)} {a.name} @ {gp(a.price)}")


def cmd_limit(a):
    """User says when an item's 4h buy limit resets (and optionally how much was used)."""
    s = load_state()
    ids = _item_ids()
    if a.name not in ids:
        sys.exit(f"Unknown GE item name: {a.name!r}")
    hh, mm = map(int, a.resets.split(":"))
    t = now()
    reset = t.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if reset <= t:
        reset += dt.timedelta(days=1)
    s.setdefault("limit_resets", {})[str(ids[a.name])] = reset.isoformat()
    if a.used is not None:
        s.setdefault("limit_used", {})[str(ids[a.name])] = a.used
    save_state(s)
    print(f"{a.name}: buy limit resets at {reset:%H:%M}" + (f", {gp(a.used)} used" if a.used is not None else ""))


def cmd_deposit(a):
    """Add (or with a negative amount, withdraw) money; raises the capital cap by the same amount."""
    s = load_state()
    s["cash"] += a.amount
    s["capital"] += a.amount
    s.setdefault("deposits", []).append({"at": now().isoformat(), "amount": a.amount, "note": a.note or ""})
    save_state(s)
    print(f"{'Added' if a.amount >= 0 else 'Withdrew'} {gp(abs(a.amount))} · cash {gp(s['cash'])} · capital {gp(s['capital'])}")


def cmd_block(a):
    s = load_state()
    bl = s["cfg"].setdefault("blacklist", [])
    if a.action == "add" and a.name not in bl:
        bl.append(a.name)
    elif a.action == "rm" and a.name in bl:
        bl.remove(a.name)
    save_state(s)
    print(f"Never trade ({len(bl)}): " + (", ".join(sorted(bl)) or "—"))


def cmd_watch(a):
    s = load_state()
    wl = s["cfg"]["watchlist"]
    if a.action == "add":
        if a.name not in _item_ids():
            sys.exit(f"Unknown GE item name: {a.name!r}")
        if a.name not in wl:
            wl.append(a.name)
        save_state(s)
    elif a.action == "rm":
        if a.name in wl:
            wl.remove(a.name)
        save_state(s)
    print(f"Watchlist ({len(wl)}): " + ", ".join(sorted(wl)))


def cmd_totals(_a):
    """Short performance summary ("Totals")."""
    s = load_state()
    start_cap = s.get("start_capital", 100_000_000) + sum(x["amount"] for x in s.get("deposits", []))
    # one trade = one sell offer (partial fills of the same offer merged)
    trades: dict = {}
    for r_ in s["realized"]:
        key = r_.get("order") or f"{r_['name']}@{r_['sell_each']}"
        t_ = trades.setdefault(key, {"name": r_["name"], "profit": 0, "cost": 0, "last": r_["at"], "item_id": r_.get("item_id")})
        t_["profit"] += r_["profit"]
        t_["cost"] += r_["buy_each"] * r_["qty"]
        t_["last"] = max(t_["last"], r_["at"])
    # trade duration: from the first buy fill of that item before the sale to the last sell fill
    buys = [(f["at"], o["name"]) for o in s["orders"] if o["side"] == "buy" for f in o["fills"]]
    durs = []
    for t_ in trades.values():
        prior = [at for at, n in buys if n == t_["name"] and at <= t_["last"]]
        if prior:
            durs.append((dt.datetime.fromisoformat(t_["last"]) - dt.datetime.fromisoformat(max(prior))).total_seconds() / 3600)
    realised = sum(t_["profit"] for t_ in trades.values())
    held_cost = sum(h["cost"] for h in s["holdings"].values() if h["qty"] > 0)
    total = s["cash"] + held_cost
    n = len(trades)
    wins = [t_ for t_ in trades.values() if t_["profit"] > 0]
    # unrealised: holdings marked at what they'd fetch now (live instant-buy side, after tax)
    unreal = None
    try:
        mk = rm.load_market()
        unreal = 0
        for k, h in s["holdings"].items():
            if h["qty"] <= 0:
                continue
            it = mk["items"].get(int(k))
            rp = rm.ref_prices(it) if it else None
            if rp:
                unreal += (rm.net_sell(rp[1], s["cfg"]["tax_rate"], h["name"]) * h["qty"]) - h["cost"]
    except Exception:
        pass
    first = s["plans"][0]["at"] if s["plans"] else None  # desk start = first plan
    days = max(hours_since(first) / 24, 1 / 24) if first else None
    sign = lambda v: ("+" if v >= 0 else "") + gpc(v)  # noqa: E731
    print(f"Total profit: {sign(realised)} realised" + (f" · {sign(unreal)} unrealised (items still held, if sold at today's prices)" if unreal is not None else ""))
    dep = sum(x["amount"] for x in s.get("deposits", []))
    started = f"put in {gpc(start_cap)}" + (f" = {gpc(start_cap - dep)} start + {gpc(dep)} added" if dep else "")
    print(f"Total money: {gpc(total)} ({started}, {sign(total - start_cap)}, {(total / start_cap - 1) * 100:+.1f}%) "
          f"= {gpc(s['cash'])} cash + {gpc(held_cost)} in items at cost")
    print(f"Trades completed: {n} · {len(wins)} won, {n - len(wins)} lost" + (f" · win rate {len(wins) / n:.0%}" if n else ""))
    if durs:
        print(f"Average trade time: {statistics.fmean(durs):.1f} h (buy filled → sold, by report times)")
    if n:
        avg = realised / n
        roi = realised / sum(t_["cost"] for t_ in trades.values())
        best = max(trades.values(), key=lambda t_: t_["profit"])
        worst = min(trades.values(), key=lambda t_: t_["profit"])
        print(f"Average profit per trade: {sign(avg)} · average return {roi:.1%} per trade")
        print(f"Best: {best['name']} {sign(best['profit'])} · Worst: {worst['name']} {sign(worst['profit'])}")
    if days:
        print(f"Pace: {sign(realised / days)} per day over {days:.1f} days")
    open_o = open_orders(s)
    print(f"In the GE now: {len(open_o)}/{SLOTS} slots ({sum(o['side'] == 'buy' for o in open_o)} buys, "
          f"{sum(o['side'] == 'sell' for o in open_o)} sells)")


def cmd_status(_a):
    s = load_state()
    rec = track_record(s)
    if rec:
        print("Track record (ranking multiplier):")
        for name, g in sorted(rec.items(), key=lambda kv: -kv[1]["mult"]):
            print(f"  {name}: ×{g['mult']:.2f}  {g['label']}")
    print(f"Cash {gp(s['cash'])} · in buy offers {gp(reserved_cash(s))} · capital cap {gp(s['capital'])}")
    real = s["realized"]
    print(f"Realised P/L {gp(sum(r['profit'] for r in real))} over {len(real)} sales")
    print("Holdings:")
    for k, h in s["holdings"].items():
        if h["qty"] > 0:
            print(f"  {h['name']}: {gp(h['qty'])} @ {gp(h['cost'] / h['qty'])} ea (since {h['since'][:16]})")
    if s["plans"]:
        p = s["plans"][-1]
        print(f"Latest plan {p['id']}:")
        for n, oid in p["map"].items():
            o = next(x for x in s["orders"] if x["id"] == oid)
            print(f"  {n}) {o['id']} {o['side']} {o['name']} {gp(filled(o))}/{gp(o['qty'])} @ {gp(o['price'])} [{o['status']}]")


def gp_arg(v: str) -> int:
    v = v.lower().replace(",", "").replace("_", "")
    mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get(v[-1], 1)
    return round(float(v[:-1] if v[-1] in "kmb" else v) * mult)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan"); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--review-all", action="store_true")
    p.add_argument("--away", type=float, metavar="DAYS", help="fill free slots with set-and-forget trades for DAYS away")
    p.set_defaults(fn=cmd_plan)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    p = sub.add_parser("fill"); p.add_argument("n", type=int)
    g = p.add_mutually_exclusive_group(); g.add_argument("--qty", type=gp_arg); g.add_argument("--to", type=gp_arg)
    p.add_argument("--price", type=gp_arg); p.add_argument("--close", action="store_true")
    p.add_argument("--no-sell", action="store_true", help="don't auto-record the follow-up sell offer")
    p.set_defaults(fn=cmd_fill)
    p = sub.add_parser("cancel"); p.add_argument("n", type=int); p.set_defaults(fn=cmd_cancel)
    p = sub.add_parser("set"); p.add_argument("--cash", type=gp_arg); p.add_argument("--capital", type=gp_arg)
    p.add_argument("--tax", type=float); p.set_defaults(fn=cmd_set)
    p = sub.add_parser("holding"); p.add_argument("name"); p.add_argument("qty", type=gp_arg)
    p.add_argument("cost_each", type=gp_arg); p.set_defaults(fn=cmd_holding)
    sub.add_parser("widget").set_defaults(fn=cmd_widget)
    sub.add_parser("totals").set_defaults(fn=cmd_totals)
    p = sub.add_parser("deposit"); p.add_argument("amount", type=gp_arg); p.add_argument("--note")
    p.set_defaults(fn=cmd_deposit)
    p = sub.add_parser("limit"); p.add_argument("name"); p.add_argument("--resets", required=True)
    p.add_argument("--used", type=gp_arg); p.set_defaults(fn=cmd_limit)
    p = sub.add_parser("block"); p.add_argument("action", nargs="?", choices=["add", "rm"])
    p.add_argument("name", nargs="?"); p.set_defaults(fn=cmd_block)
    p = sub.add_parser("edit"); p.add_argument("n", type=int); p.add_argument("--price", type=gp_arg)
    p.add_argument("--qty", type=gp_arg); p.add_argument("--pin", action="store_true")
    p.add_argument("--slot", type=int, choices=range(1, SLOTS + 1)); p.set_defaults(fn=cmd_edit)
    p = sub.add_parser("add"); p.add_argument("side", choices=["buy", "sell"]); p.add_argument("name")
    p.add_argument("qty", type=gp_arg); p.add_argument("price", type=gp_arg)
    p.add_argument("--slot", type=int, choices=range(1, SLOTS + 1)); p.set_defaults(fn=cmd_add)
    p = sub.add_parser("watch"); p.add_argument("action", nargs="?", choices=["add", "rm"])
    p.add_argument("name", nargs="?"); p.set_defaults(fn=cmd_watch)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
