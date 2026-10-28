"""Shared RS3 GE helpers for the trade desk (stdlib only).

Data: the RuneScape Wiki real-time prices API (Jagex trade stream, refreshed every 60 s):
  https://prices.runescape.wiki/api/v2/rs/{mapping,latest,1h,24h}       bulk, all items
  https://prices.runescape.wiki/api/v2/rs/timeseries?lookback=7d&id=ID  hourly, one item
Docs: https://runescape.wiki/w/RuneScape:Real-time_Prices  (send a descriptive User-Agent;
never loop /latest?id= over items — use the bulk call).

  high = instant-buy price  (what SELL offers get filled at)
  low  = instant-sell price (what BUY offers get filled at)
"""
from __future__ import annotations

import json
import math
import statistics
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "rs3-flip-finder/0.3 (personal GE planner; github.com/FlamingFlamingo-net)"
RT = "https://prices.runescape.wiki/api/v2/rs"

# Grinding recipes (pestle and mortar, Herblore 1, members): (input, inputs per output, output).
# Source: wiki recipe infoboxes on each Powder of ... page.
RECIPES = [
    ("Dazzling whirligig shell", 1, "Powder of penance"),
    ("Speedy whirligig shell", 15, "Powder of burials"),
    ("Plain whirligig shell", 15, "Powder of defence"),
    ("Swift whirligig shell", 15, "Powder of item protection"),
    ("Gliding whirligig shell", 15, "Powder of protection"),
    ("Hasty whirligig shell", 15, "Powder of pulverising"),
]

# The user's proven markets: always analysed, small ranking bonus (still must be profitable).
DEFAULT_WATCHLIST = sorted({
    "Golden roses",
    *(n for r in RECIPES for n in (r[0], r[2])),
    # Calculator:Prayer/Bones
    "Bones", "Big bones", "Baby dragon bones", "Wyvern bones", "Dragon bones", "Dagannoth bones",
    "Airut bones", "Ourg bones", "Hardened dragon bones", "Dragonkin bones", "Dinosaur bones",
    "Frost dragon bones", "Reinforced dragon bones", "Fayrg bones", "Raurg bones", "Shaikahan bones",
    "Zogre bones", "Jogre bones", "Wolf bones", "Bat bones", "Monkey bones", "Burnt bones",
    "Impious ashes", "Accursed ashes", "Infernal ashes", "Tortured ashes", "Searing ashes",
})


def get_json(url: str, timeout: int = 40):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _retry(fn, tries=3):
    for k in range(tries):
        try:
            return fn()
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(1.5 * (k + 1))


# ---------- tax ----------
def ge_tax(sell: int, rate: float, name: str = "") -> int:
    """Seller pays floor(price*rate) per item; <50 gp and Bonds exempt (wiki, 2 % since 2023-01-09)."""
    if name == "Bond" or sell < 50:
        return 0
    return math.floor(sell * rate)


def ge_screen_total(side: str, qty: int, price: int, rate: float, name: str = "") -> int:
    """The coin total the GE offer screen shows before you confirm. Buy: qty x price.
    Sell: what you'll RECEIVE, i.e. qty x price minus 2 % taken on the whole total, rounded down
    (checked in game: 6 x 3,679,973 shows 21,638,242)."""
    gross = qty * price
    if side != "sell" or ge_tax(price, rate, name) == 0:
        return gross
    return gross - math.floor(gross * rate)


def net_sell(sell, rate, name=""):
    return sell - ge_tax(sell, rate, name)


def break_even_sell(buy_each: float, rate: float, name: str = "") -> int:
    s = max(math.floor(buy_each) + 1, math.floor(buy_each / (1 - rate)) - 2)
    while net_sell(s, rate, name) <= buy_each:
        s += 1
    return s


# ---------- market snapshot ----------
def load_market() -> dict:
    with ThreadPoolExecutor(4) as ex:
        fut = {k: ex.submit(_retry, lambda k=k: get_json(f"{RT}/{k}")) for k in ("mapping", "latest", "1h", "24h")}
        mapping = fut["mapping"].result()
        latest = fut["latest"].result()["data"]
        h1 = fut["1h"].result()["data"]
        try:
            d24 = fut["24h"].result()["data"]
        except Exception:  # undocumented for RS3; fall back to 1h × 24
            d24 = {k: {**v, "lowPriceVolume": (v.get("lowPriceVolume") or 0) * 24,
                       "highPriceVolume": (v.get("highPriceVolume") or 0) * 24} for k, v in h1.items()}
    items = {}
    for m in mapping:
        i, k = m["id"], str(m["id"])
        l, a, b = latest.get(k) or {}, h1.get(k) or {}, d24.get(k) or {}
        items[i] = {
            "id": i, "name": m["name"], "limit": m.get("limit") or 0, "members": m.get("members", True),
            "highalch": m.get("highalch") or 0,
            "low": l.get("low"), "high": l.get("high"), "lowTime": l.get("lowTime"), "highTime": l.get("highTime"),
            "l1": a.get("avgLowPrice"), "h1": a.get("avgHighPrice"),
            "l24": b.get("avgLowPrice"), "h24": b.get("avgHighPrice"),
            "lv24": b.get("lowPriceVolume") or 0, "hv24": b.get("highPriceVolume") or 0,
        }
    return {"items": items, "by_name": {it["name"]: it for it in items.values()}, "fetched": int(time.time())}


def _med(xs):
    xs = [x for x in xs if x]
    return statistics.median(xs) if xs else None


def ref_prices(it: dict, st: dict | None = None):
    """(our buy offer, our sell offer): 1 gp inside the robust instant-sell / instant-buy prices.
    Median of latest, 1h avg and 24h avg so one odd trade can't move it; with history, also
    bounded by the last 24 h volume-weighted prices (buy no lower, sell no higher) so a few
    stray trades can't fake a wide margin."""
    lo, hi = _med([it["low"], it["l1"], it["l24"]]), _med([it["high"], it["h1"], it["h24"]])
    if st and st.get("vw_low24") and st.get("vw_high24"):
        lo = max(lo or 0, st["vw_low24"]) or None
        hi = min(hi or math.inf, st["vw_high24"])
    if not lo or not hi or hi == math.inf:
        return None
    return math.ceil(lo) + 1, math.floor(hi) - 1


# ---------- 7-day hourly history ----------
def load_series(ids, workers: int = 6) -> dict:
    """{id: hourly points}; None when the call kept failing."""
    def one(i):
        try:
            return i, _retry(lambda: get_json(f"{RT}/timeseries?lookback=7d&id={i}"))["data"]
        except Exception:
            return i, None
    with ThreadPoolExecutor(workers) as ex:
        return dict(ex.map(one, list(ids)))


def series_stats(points, rate: float, name: str = "") -> dict | None:
    if not points:
        return None
    pts = sorted(points, key=lambda p: p["timestamp"])
    both = [p for p in pts if p.get("avgHighPrice") and p.get("avgLowPrice")]
    if len(both) < 24:
        return None
    mids = [(p["avgHighPrice"] + p["avgLowPrice"]) / 2 for p in both]
    days = max(1.0, (pts[-1]["timestamp"] - pts[0]["timestamp"]) / 86400)
    recent, prev = mids[-6:], mids[-12:-6]
    cut = pts[-1]["timestamp"] - 86400
    day = [p for p in pts if p["timestamp"] > cut]

    def vwap(price_key, vol_key):
        v = sum(p.get(vol_key) or 0 for p in day if p.get(price_key))
        return sum(p[price_key] * (p.get(vol_key) or 0) for p in day if p.get(price_key)) / v if v else None
    return {
        "vw_low24": vwap("avgLowPrice", "lowPriceVolume"),
        "vw_high24": vwap("avgHighPrice", "highPriceVolume"),
        # share of hours where selling at the avg high beat buying at the avg low, after tax
        "cons": statistics.fmean(
            1.0 if p["avgHighPrice"] - ge_tax(p["avgHighPrice"], rate, name) > p["avgLowPrice"] else 0.0 for p in both),
        "mean7": statistics.fmean(mids),
        "sd7": statistics.stdev(mids),
        "avg_high7": statistics.fmean(p["avgHighPrice"] for p in both),
        "ch24": statistics.fmean(mids[-24:]) / statistics.fmean(mids) - 1,
        "ch6h": statistics.fmean(recent) / statistics.fmean(prev) - 1 if prev else 0.0,
        "lowflow": sum(p.get("lowPriceVolume") or 0 for p in pts) / days,    # instant-sells/day -> fills our buys
        "highflow": sum(p.get("highPriceVolume") or 0 for p in pts) / days,  # instant-buys/day -> fills our sells
        "hourly": {p["timestamp"]: (p.get("avgHighPrice"), p.get("avgLowPrice")) for p in pts},
    }


def _flags(it, st, buy, sell, flow):
    f = []
    if st["cons"] < 0.6: f.append("unreliable margin")
    if st["ch24"] < -0.05: f.append("falling")
    if st["ch24"] > 0.10: f.append("spiking")
    if flow < 200: f.append("thin")
    times = [t for t in (it["lowTime"], it["highTime"]) if t]
    if not times or time.time() - min(times) > 6 * 3600: f.append("stale prices")
    if sell > buy * 1.25: f.append("wide spread")
    week_daily = st["lowflow"] + st["highflow"]
    if week_daily and it["lv24"] + it["hv24"] > 3 * week_daily: f.append("volume spike")  # pump-and-dump smell
    return f


def confidence(flags):
    c = 1.0
    for x in flags:
        if x == "alch floor":
            c *= 1.1  # high alch value close under the buy price caps the downside
        else:
            c *= 0.4 if x in ("thin", "stale prices", "unreliable margin") else 0.6
    return min(c, 1.0)


def slot_hours(qty, lv24, lowflow, hv24, highflow, cfg, ratio=1, out_qty=None):
    """Expected hours a GE slot is busy: buy side fills against instant-sells, sell side against
    instant-buys, each at our `capture` share of the flow."""
    buy_rate = cfg["capture"] * min(lv24, lowflow) / 24
    sell_rate = cfg["capture"] * min(hv24, highflow) / 24
    if buy_rate <= 0 or sell_rate <= 0:
        return 48.0
    return qty / buy_rate + (out_qty if out_qty is not None else qty) / sell_rate


def suggest(it, st, cfg, budget, watch=False) -> list[dict]:
    """Spread flips (buy at instant-sell+1, sell at instant-buy-1) and dip buys for one item."""
    rp = ref_prices(it, st)
    if not rp or not st:
        return []
    buy, sell = rp
    flow = min(min(it["lv24"], st["lowflow"]), min(it["hv24"], st["highflow"]))
    limit = it["limit"] if it["limit"] > 0 else math.inf
    hold = cfg["hold_hours"] / 24
    flags = _flags(it, st, buy, sell, flow)
    out = []

    def push(strategy, b, s_, extra=()):
        profit = net_sell(s_, cfg["tax_rate"], it["name"]) - b
        qty = math.floor(min(limit, budget / b, cfg["capture"] * flow * hold)) if b > 0 else 0
        if profit <= 0 or qty < 1 or profit / b < cfg.get("min_roi", 0):
            return
        fl = flags + list(extra)
        if it.get("highalch") and b <= it["highalch"] * 1.1:
            fl.append("alch floor")
        conf = confidence(fl)
        hours = slot_hours(qty, it["lv24"], st["lowflow"], it["hv24"], st["highflow"], cfg)
        out.append({"strategy": strategy, "item": it, "buy": b, "sell": s_, "profit": profit, "qty": qty,
                    "total": profit * qty, "roi": profit / b, "flags": fl, "confidence": conf, "hours": hours,
                    "score": profit * qty * conf * (1.2 if watch else 1) / max(hours, cfg.get("min_slot_hours", 6)),
                    "watch": watch})

    # Margins over 15 % are almost always a few stray trades in a thin market: they don't fill.
    if st["cons"] >= 0.5 and sell <= buy * 1.15:
        push("spread", buy, sell)
    mid = (buy + sell) / 2
    if st["sd7"] > 0 and (mid - st["mean7"]) / st["sd7"] <= -cfg["dip_z"] and mid < st["mean7"] * 0.97:
        push("dip", buy, math.floor(st["avg_high7"]) - 1, ["still dropping"] if st["ch6h"] < -0.02 else [])
    return out


def suggest_process(recipe, mk, stats, cfg, budget, watch=True) -> dict | None:
    """Buy inputs, grind, sell the output. qty is in INPUT items."""
    inp, ratio, outp = recipe
    a, b = mk["by_name"].get(inp), mk["by_name"].get(outp)
    if not a or not b:
        return None
    sa, sb = stats.get(a["id"]), stats.get(b["id"])
    if not sa or not sb:
        return None
    ra, rb = ref_prices(a, sa), ref_prices(b, sb)
    if not ra or not rb:
        return None
    in_buy, out_sell = ra[0], rb[1]
    rate = cfg["tax_rate"]
    profit_out = net_sell(out_sell, rate, outp) - ratio * in_buy
    hold = cfg["hold_hours"] / 24
    inflow, outflow = min(a["lv24"], sa["lowflow"]), min(b["hv24"], sb["highflow"])
    n = math.floor(min((a["limit"] or math.inf) / ratio, budget / (ratio * in_buy),
                       cfg["capture"] * outflow * hold, cfg["capture"] * inflow * hold / ratio))
    if profit_out <= 0 or n < 1 or profit_out / (ratio * in_buy) < cfg.get("min_roi", 0):
        return None
    # week consistency, hour by hour: output at its avg high vs inputs at their avg low
    common = sb["hourly"].keys() & sa["hourly"].keys()
    hrs = [(sb["hourly"][t][0], sa["hourly"][t][1]) for t in common if sb["hourly"][t][0] and sa["hourly"][t][1]]
    cons = statistics.fmean(1.0 if h - ge_tax(h, rate, outp) > ratio * l else 0.0 for h, l in hrs) if len(hrs) >= 24 else 0.0
    if cons < 0.5:
        return None
    flags = []
    if cons < 0.6: flags.append("unreliable margin")
    if sb["ch24"] < -0.05: flags.append("output falling")
    if min(inflow / ratio, outflow) < 50: flags.append("thin")
    conf = confidence(flags)
    buy_rate = cfg["capture"] * inflow / 24
    sell_rate = cfg["capture"] * outflow / 24
    hours = (n * ratio / buy_rate + n / sell_rate) if buy_rate > 0 and sell_rate > 0 else 48.0
    return {"strategy": "process", "item": a, "out": b, "ratio": ratio, "buy": in_buy, "sell": out_sell,
            "qty": n * ratio, "outputs": n, "profit": profit_out / ratio, "profit_out": profit_out,
            "total": profit_out * n, "roi": profit_out / (ratio * in_buy), "flags": flags, "cons": cons,
            "confidence": conf, "hours": hours, "watch": watch,
            "score": profit_out * n * conf * (1.2 if watch else 1) / max(hours, cfg.get("min_slot_hours", 6))}


# ---------- "away" trades: buy now, park a sell at a level the price keeps revisiting ----------
def load_series30(ids, workers: int = 6) -> dict:
    """30 days at 6-hour steps; {id: points or None}."""
    def one(i):
        try:
            return i, _retry(lambda: get_json(f"{RT}/timeseries?lookback=30d&id={i}"))["data"]
        except Exception:
            return i, None
    with ThreadPoolExecutor(workers) as ex:
        return dict(ex.map(one, list(ids)))


def away_analysis(points, buy: int, rate: float, name: str, days: float) -> dict | None:
    """Look back 30 days: every time the price sat near today's buy level, did it climb to a
    sell target within `days`? Picks the target with the best (hit rate x net profit).
    Uses 6-hour AVERAGE highs, so spikes count conservatively."""
    pts = sorted((p for p in points or [] if p.get("avgHighPrice") or p.get("avgLowPrice")),
                 key=lambda p: p["timestamp"])
    if len(pts) < 60:
        return None
    w = max(1, round(days * 4))  # 6-hour steps
    entries = []
    for i in range(len(pts) - w):
        lo = pts[i].get("avgLowPrice") or pts[i].get("avgHighPrice")
        if lo and lo <= buy * 1.02:  # a moment when you could have bought at about today's price
            ahead = pts[i + 1:i + 1 + w]
            highs = [p["avgHighPrice"] for p in ahead if p.get("avgHighPrice")]
            lows = [p["avgLowPrice"] for p in ahead if p.get("avgLowPrice")]
            if highs:
                entries.append((max(highs), min(lows) if lows else lo))
    if len(entries) < 8:
        return None
    best = None
    for pct in range(2, 31):
        target = math.floor(buy * (1 + pct / 100))
        net = net_sell(target, rate, name) - buy
        if net <= 0:
            continue
        hit = sum(1 for mx, _ in entries if mx >= target) / len(entries)
        if hit >= 0.6 and (best is None or hit * net > best["hit"] * best["net"]):
            best = {"target": target, "net": net, "hit": hit}
    if not best:
        return None
    mids = [((p.get("avgHighPrice") or p["avgLowPrice"]) + (p.get("avgLowPrice") or p["avgHighPrice"])) / 2 for p in pts]
    cur = mids[-1]
    best.update(
        entries=len(entries),
        worst_dip=1 - min(mn for _, mn in entries) / buy,     # deepest fall below the buy while waiting
        pct_rank=sum(1 for m in mids if m <= cur) / len(mids),  # where today sits in the 30-day range
        trend30=mids[-1] / statistics.fmean(mids[:8]) - 1,
    )
    return best
