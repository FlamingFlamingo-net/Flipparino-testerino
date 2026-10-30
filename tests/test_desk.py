"""Tests for the trade desk's money logic (stdlib unittest, no network).

  python -m unittest discover -s tests -v        (from the repo root)

Every test runs against a throwaway ledger in a temp dir; the real ledger is never touched.
The market and history APIs are replaced by small fixtures, so results are deterministic.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import math
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import desk  # noqa: E402
import machines as mc  # noqa: E402
import rsmarket as rm  # noqa: E402

RATE = 0.02


# ---------------- fixtures ----------------
def item(i, name, low, high, limit, vol=50_000):
    now = int(time.time())
    return {"id": i, "name": name, "limit": limit, "members": True, "highalch": 0,
            "low": low, "high": high, "lowTime": now - 60, "highTime": now - 60,
            "l1": low, "h1": high, "l24": low, "h24": high, "lv24": vol, "hv24": vol}


def market(*items):
    its = {it["id"]: it for it in items}
    return {"items": its, "by_name": {it["name"]: it for it in its.values()}, "fetched": int(time.time())}


def hourly(low, high, hours=168, vol=2_000):
    t0 = int(time.time()) // 3600 * 3600 - hours * 3600
    return [{"timestamp": t0 + h * 3600, "avgLowPrice": low, "avgHighPrice": high,
             "lowPriceVolume": vol, "highPriceVolume": vol} for h in range(hours)]


FAKE_FILL = {"items": [{"item": "Speedy whirligig shell", "qty": 8_000, "p10": 900, "median": 1_000,
                        "buy_at": 20_001, "timing": None}], "filled": 8_000, "need": 8_400,
             "worst_week": 7_200_000, "typical_week": 8_000_000, "cash": 160_008_000, "score": 4_000_000}
FAKE_MACHINE_REPORT = {
    "at": "2026-10-28T09:00", "dc_each": 68_000, "nature": 380, "missing_history": 0, "setups": {},
    "machines": {"alch": {**mc.MACHINES["alch"], "per_week": 4_200, "items": [], "current": [],
                          "fill": {1: FAKE_FILL, 2: FAKE_FILL}},
                 "disasm": {**mc.MACHINES["disasm"], "per_week": 10_080, "items": [], "current": [],
                            "fill": {1: {**FAKE_FILL, "items": []}}}},
}


class LedgerCase(unittest.TestCase):
    """Points desk.py at a temp ledger and fakes the price APIs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = (desk.LEDGER, desk.STATE, rm.load_market, rm.load_series, rm.load_series30,
                       rm.get_json, desk._item_ids, mc.analyse, desk.now)
        desk.LEDGER = self.tmp / "ledger"
        desk.STATE = desk.LEDGER / "state.json"
        self.mk = market(
            item(1, "Bones", 1_000, 1_100, 10_000),
            item(2, "Speedy whirligig shell", 20_000, 21_000, 5_000),
            item(3, "Powder of burials", 380_000, 400_000, 100),
            item(4, "Slayer Wildcard", 100_000, 110_000, 100),
        )
        series = {1: hourly(1_000, 1_100), 2: hourly(20_000, 21_000), 3: hourly(380_000, 400_000),
                  4: hourly(100_000, 110_000)}
        rm.load_market = lambda: self.mk
        rm.load_series = lambda ids, workers=6: {i: series.get(i) for i in ids}
        rm.load_series30 = lambda ids, workers=6: {i: None for i in ids}
        desk._item_ids = lambda: {it["name"]: i for i, it in self.mk["items"].items()}
        # machines: no network; a fixed weekly shopping list (Speedy whirligig shell stands in for an input)
        mc.analyse = lambda *a, **k: FAKE_MACHINE_REPORT
        self._sched = mc.DEFAULT_SCHEDULE  # no machines in the generic ledger tests
        mc.DEFAULT_SCHEDULE = {**self._sched, "setup": {}}
        # default clock: a Wednesday morning, outside the machine buy window
        self.clock = dt.datetime(2026, 10, 28, 9, 0).astimezone()
        desk.now = lambda: self.clock

    def tearDown(self):
        (desk.LEDGER, desk.STATE, rm.load_market, rm.load_series, rm.load_series30,
         rm.get_json, desk._item_ids, mc.analyse, desk.now) = self._saved
        mc.DEFAULT_SCHEDULE = self._sched
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cmd(self, fn, **kw) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(argparse.Namespace(**kw))
        return buf.getvalue()

    def plan(self, **kw):
        args = {"dry_run": False, "review_all": False, "away": None, "next": None, **kw}
        return self.run_cmd(desk.cmd_plan, **args)

    def fill(self, n, qty=None, to=None, price=None, close=False, no_sell=False):
        return self.run_cmd(desk.cmd_fill, n=n, qty=qty, to=to, price=price, close=close, no_sell=no_sell)

    def age(self, hours):
        """Pretend `hours` passed: shift every timestamp in the ledger back."""
        s = desk.load_state()
        back = lambda iso: (dt.datetime.fromisoformat(iso) - dt.timedelta(hours=hours)).isoformat()  # noqa: E731
        for o in s["orders"]:
            o["placed"] = back(o["placed"])
            for f in o["fills"]:
                f["at"] = back(f["at"])
        for h in s["holdings"].values():
            if h.get("since"):
                h["since"] = back(h["since"])
        s["limit_resets"] = {k: back(v) for k, v in s.get("limit_resets", {}).items()}
        desk.save_state(s)


# ---------------- pure maths ----------------
class TaxTests(unittest.TestCase):
    def test_wiki_example(self):
        # Wiki: seller sets 4,992 each, buyer pays 4,992, seller receives 4,893 (99 tax)
        self.assertEqual(rm.ge_tax(4_992, RATE), 99)
        self.assertEqual(rm.net_sell(4_992, RATE), 4_893)

    def test_exemptions(self):
        self.assertEqual(rm.ge_tax(49, RATE), 0)
        self.assertEqual(rm.ge_tax(50, RATE), 1)
        self.assertEqual(rm.ge_tax(138_000_000, RATE, "Bond"), 0)

    def test_ge_screen_total_matches_game(self):
        # Seen in game: selling 6 x 3,679,973 shows 21,638,242 (tax on the whole total, floored)
        self.assertEqual(rm.ge_screen_total("sell", 6, 3_679_973, RATE), 21_638_242)
        self.assertEqual(rm.ge_screen_total("sell", 2, 3_679_973, RATE), 7_212_748)  # "received 7,212,748"
        self.assertEqual(rm.ge_screen_total("buy", 6, 3_511_462, RATE), 21_068_772)
        self.assertEqual(rm.ge_screen_total("sell", 1_000, 40, RATE), 40_000)  # under 50 gp: no tax

    def test_break_even_is_minimal(self):
        for buy in (51, 999, 4_537, 15_300, 764_716, 3_272_192):
            be = rm.break_even_sell(buy, RATE)
            self.assertGreater(rm.net_sell(be, RATE), buy)
            self.assertLessEqual(rm.net_sell(be - 1, RATE), buy)
        self.assertEqual(rm.break_even_sell(15_300, RATE), 15_613)  # the Marmaros ore case


class QtyRoundingTests(unittest.TestCase):
    def test_hundreds(self):
        self.assertEqual(desk.round_qty(6_038, 10_000, 1e9, 2_070), 6_000)
        self.assertEqual(desk.round_qty(1_026, 10_000, 1e9, 12_174), 1_000)

    def test_caps_round_down(self):
        # nearest is 2,200 but cash only covers 2,187 -> 2,100
        self.assertEqual(desk.round_qty(2_187, 0, 12_500_000, 5_715), 2_100)
        self.assertEqual(desk.round_qty(150, 100, 1e9, 1), 100)  # never above the buy limit

    def test_small_kept_exact(self):
        self.assertEqual(desk.round_qty(17, 1_000, 1e9, 722_347), 17)

    def test_process_step(self):
        self.assertEqual(desk.round_qty(1_390, 5_000, 1e9, 20_000, step=300), 1_500)


class RefPriceTests(unittest.TestCase):
    def test_one_gp_inside_the_spread(self):
        it = item(1, "X", 1_000, 1_100, 100)
        self.assertEqual(rm.ref_prices(it), (1_001, 1_099))

    def test_outlier_latest_trade_ignored(self):
        it = item(1, "X", 1_000, 1_100, 100)
        it["high"] = 9_999  # one silly instant-buy
        self.assertEqual(rm.ref_prices(it)[1], 1_099)


class AwayAnalysisTests(unittest.TestCase):
    def series(self, f, n=120):
        t0 = int(time.time()) - n * 21_600
        return [{"timestamp": t0 + k * 21_600, "avgLowPrice": f(k) * 0.99, "avgHighPrice": f(k) * 1.01,
                 "lowPriceVolume": 1_000, "highPriceVolume": 1_000} for k in range(n)]

    def test_regular_cycle_finds_target(self):
        pts = self.series(lambda k: 1_000 * (1 + 0.06 * math.sin(k * 2 * math.pi / 8)))  # 2-day cycle, ±6 %
        a = rm.away_analysis(pts, 960, RATE, "X", 3)
        self.assertIsNotNone(a)
        self.assertGreaterEqual(a["hit"], 0.6)
        self.assertGreater(a["net"], 0)

    def test_flat_market_has_no_target(self):
        self.assertIsNone(rm.away_analysis(self.series(lambda k: 1_000), 1_000, RATE, "X", 3))


# ---------------- ledger flows ----------------
class FillTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.plan()
        self.s = desk.load_state()

    def num(self, name, side="buy"):
        s = desk.load_state()
        for n, oid in s["plans"][-1]["map"].items():
            o = next(x for x in s["orders"] if x["id"] == oid)
            if o["name"] == name and o["side"] == side and o["status"] == "open":
                return int(n), o
        self.fail(f"{side} {name} not in plan")

    def test_plan_respects_slots_and_cash(self):
        open_o = desk.open_orders(self.s)
        self.assertLessEqual(len(open_o), desk.SLOTS)
        self.assertLessEqual(desk.reserved_cash(self.s), self.s["cash"])

    def test_full_buy_then_sell_books_profit_after_tax(self):
        n, o = self.num("Bones")
        out = self.fill(n)
        self.assertIn("Sell offer recorded", out)  # user's workflow: list at the plan price straight away
        s = desk.load_state()
        self.assertEqual(s["holdings"]["1"]["qty"], o["qty"])
        n_sell, sell = self.num("Bones", "sell")
        self.assertEqual(sell["price"], o["target_sell"])
        self.fill(n_sell)
        s = desk.load_state()
        r = s["realized"][-1]
        expect = (sell["price"] - rm.ge_tax(sell["price"], RATE)) * o["qty"] - o["price"] * o["qty"]
        self.assertEqual(r["profit"], expect)
        self.assertEqual(s["cash"], 100_000_000 + expect)
        self.assertEqual(s["holdings"]["1"]["qty"], 0)

    def test_partial_fill_and_overfill_guard(self):
        n, o = self.num("Bones")
        self.fill(n, to=100, price=999, no_sell=True)
        s = desk.load_state()
        self.assertEqual(s["holdings"]["1"]["cost"], 100 * 999)
        with self.assertRaises(SystemExit):
            self.fill(n, to=o["qty"] + 1)

    def test_buy_limit_window(self):
        n, o = self.num("Bones")
        part = o["qty"] // 2
        self.fill(n, to=part, no_sell=True)
        left, wait, clock = desk.limit_state(desk.load_state(), self.mk["items"][1])
        self.assertEqual(left, 10_000 - part)
        self.assertAlmostEqual(wait, 4, delta=0.05)

    def test_widget_change_row_shows_latest_price(self):
        """Two re-prices in the same half-day: the panel must show the newest price, not the first."""
        n, o = self.num("Bones")
        self.age(12)
        self.mk["items"][1].update(low=1_050, l1=1_050, l24=1_050, high=1_150, h1=1_150, h24=1_150)
        self.plan(review_all=True)
        self.age(1)
        self.mk["items"][1].update(low=1_060, l1=1_060, l24=1_060, high=1_160, h1=1_160, h24=1_160)
        self.plan(review_all=True)
        s = desk.load_state()
        ids = [p["id"] for p in s["plans"]]
        self.assertEqual(len(ids), len(set(ids)), "plan ids must be unique")
        live = next(x for x in s["orders"] if x["id"] == o["id"])
        self.assertEqual(live["price"], 1_061)  # re-priced twice
        row = next(r for r in desk.plan_rows(s, s["plans"][-1]) if r["item"] == "Bones")
        self.assertEqual((row["verb"], row["price"]), ("CHANGE", 1_061))

    def test_next_checkin_sizes_for_the_gap_and_is_not_saved(self):
        self.assertAlmostEqual(desk.hours_until(self.clock, "07:00"), 22.0)  # Wed 09:00 -> Thu 07:00
        out = self.plan(next="07:00")
        self.assertIn("next check-in at **07:00** (~22h)", out)
        s = desk.load_state()
        self.assertEqual((s["cfg"]["hold_hours"], s["cfg"]["min_slot_hours"]), (12, 6))  # one plan only
        self.assertTrue(s["next_checkin"].startswith("2026-10-29T07:00"))

    def test_changes_vs_last_report_are_flagged(self):
        self.plan()
        s = desk.load_state()
        n, o = self.num("Bones")
        self.run_cmd(desk.cmd_edit, n=n, price=1_005, qty=9_000, pin=True, slot=None)  # user placed it differently
        self.age(1)
        self.plan()  # pinned offer is kept, but it deviates from the last report
        s = desk.load_state()
        rows = desk.plan_rows(s, s["plans"][-1])
        desk.mark_changes(s, s["plans"][-1], rows)
        row = next(r for r in rows if r["item"] == "Bones")
        got = {c["what"]: (c["from"], c["to"]) for c in row["changes"]}
        self.assertEqual(got["price"], (o["price"], 1_005))
        self.assertEqual(got["qty"], (o["qty"], 9_000))
        self.assertTrue(all("changes" not in r for r in rows if r["item"] != "Bones" and r["verb"] == "KEEP"))
        html = (desk.LEDGER / desk.BOARD).read_text(encoding="utf-8")
        self.assertIn('"what": "price"', html)

    def test_kept_buy_still_shows_its_sell_price(self):
        n, o = self.num("Bones")
        self.age(1)  # not stale: the next plan keeps it
        out = self.plan()
        self.assertIn(f"when bought, sell @ {o['target_sell']:,}", out)
        s = desk.load_state()
        row = next(r for r in desk.plan_rows(s, s["plans"][-1]) if r["item"] == "Bones")
        self.assertEqual((row["verb"], row["then_sell"]), ("KEEP", o["target_sell"]))

    def test_board_follows_every_ledger_change(self):
        board = desk.LEDGER / desk.BOARD
        self.assertTrue(board.exists(), "a plan writes the side-panel board")
        n, o = self.num("Bones")
        self.fill(n, to=7, no_sell=True)
        html = board.read_text(encoding="utf-8")
        self.assertIn('"filled": 7', html)
        self.assertIn('"live": true', html)

    def test_slots_stable_and_inherited(self):
        s = desk.load_state()
        slots = [o["slot"] for o in desk.open_orders(s)]
        self.assertEqual(len(slots), len(set(slots)), "each GE slot holds one offer")
        self.assertTrue(all(1 <= k <= desk.SLOTS for k in slots))
        n, o = self.num("Bones")
        out = self.fill(n)  # finished buy -> collected and relisted in the same slot
        self.assertIn(f"(slot {o['slot']})", out)
        self.age(1)
        self.plan()
        s = desk.load_state()
        bones_sell = next(x for x in s["orders"] if x["side"] == "sell" and x["name"] == "Bones")
        self.assertEqual(bones_sell["slot"], o["slot"])
        slots = [x["slot"] for x in desk.open_orders(s)]
        self.assertEqual(len(slots), len(set(slots)))

    def test_pinned_offer_survives_review(self):
        n, o = self.num("Bones")
        self.run_cmd(desk.cmd_edit, n=n, price=1, qty=None, pin=True)  # absurd price on purpose
        self.age(12)
        self.plan(review_all=True)
        s = desk.load_state()
        kept = next(x for x in s["orders"] if x["id"] == o["id"])
        self.assertEqual((kept["status"], kept["price"]), ("open", 1))


class ProcessTests(LedgerCase):
    def test_shells_are_ground_then_sold(self):
        self.plan()
        s = desk.load_state()
        n, o = next((int(k), x) for k, v in s["plans"][-1]["map"].items()
                    for x in s["orders"] if x["id"] == v and x["strategy"] == "process")
        self.assertEqual(o["qty"] % 15, 0)
        self.fill(n)  # grind-first orders must NOT auto-list the shells for sale
        self.assertFalse(any(x["side"] == "sell" and x["name"] == "Speedy whirligig shell"
                             for x in desk.load_state()["orders"]))
        self.age(1)
        out = self.plan()
        self.assertIn("first grind", out)
        s = desk.load_state()
        powders = s["holdings"]["3"]
        self.assertEqual(powders["qty"], o["qty"] // 15)
        self.assertAlmostEqual(powders["cost"], o["qty"] * o["price"], delta=1)
        self.assertEqual(s["holdings"]["2"]["qty"], 0)


class BlockAndRecordTests(LedgerCase):
    def test_blocked_item_never_planned(self):
        self.run_cmd(desk.cmd_block, action="add", name="Slayer Wildcard")
        self.plan()
        self.assertFalse(any(o["name"] == "Slayer Wildcard" for o in desk.load_state()["orders"]))

    def test_track_record_scales_wins_by_return(self):
        s = desk.load_state()
        now = desk.now().isoformat()
        s["realized"] = [
            {"at": now, "name": "Big", "qty": 1, "buy_each": 100, "sell_each": 110, "tax": 2, "profit": 8},
            {"at": now, "name": "Scrape", "qty": 1, "buy_each": 10_000, "sell_each": 10_210, "tax": 204, "profit": 6},
            {"at": now, "name": "Loser", "qty": 1, "buy_each": 100, "sell_each": 90, "tax": 1, "profit": -11},
        ]
        rec = desk.track_record(s)
        self.assertAlmostEqual(rec["Big"]["mult"], 1.25)
        self.assertLess(rec["Scrape"]["mult"], 1.05)
        self.assertLess(rec["Loser"]["mult"], 1.0)


if __name__ == "__main__":
    unittest.main()


# ---------------- invention machines ----------------
class MachineScheduleTests(unittest.TestCase):
    def test_one_item_per_machine_type(self):
        rows = [{"item": "A", "per_week_max": 3_000, "p10": 1_500, "median": 1_600, "buy_at": 7_000, "adj": 1_360, "timing": None},
                {"item": "B", "per_week_max": 40_000, "p10": 1_200, "median": 1_400, "buy_at": 7_000, "adj": 1_060, "timing": None}]
        f = mc.fill_plan(rows, 8_400, 1)
        self.assertEqual([(p["item"], p["qty"]) for p in f["items"]], [("B", 8_400)])  # fills the machine beats 3,000 of A

    def test_next_refill_is_tuesday_18(self):
        sch = mc.DEFAULT_SCHEDULE
        wed = dt.datetime(2026, 10, 28, 9, 0).astimezone()
        self.assertEqual(mc.next_refill(wed, sch).strftime("%a %d %H:%M"), "Tue 03 18:00")
        tue_late = dt.datetime(2026, 11, 3, 18, 30).astimezone()  # just after a refill: next week's
        self.assertEqual(mc.next_refill(tue_late, sch).strftime("%a %d %H:%M"), "Tue 10 18:00")

    def test_buy_window_is_the_days_before_refill(self):
        self.assertEqual(mc.DEFAULT_SCHEDULE["buy_days"], 7)  # default: reset to reset, buy all week
        sch = {**mc.DEFAULT_SCHEDULE, "buy_days": 3.5}  # a shorter window still works: from Saturday 06:00
        self.assertFalse(mc.in_buy_window(dt.datetime(2026, 10, 31, 5, 0).astimezone(), sch))
        self.assertTrue(mc.in_buy_window(dt.datetime(2026, 10, 31, 7, 0).astimezone(), sch))
        self.assertTrue(mc.in_buy_window(dt.datetime(2026, 11, 3, 17, 0).astimezone(), sch))

    def test_volume_cap_uses_a_slow_day(self):
        # 90 days: most days 10,000 sellers, but a quarter of days only 2,000 -> plan around the slow days
        day = 86_400
        pts = [(i * day, 100, 110, 2_000 if i % 4 == 0 else 10_000, 500) for i in range(90)]
        it = {"limit": 100_000}
        share = 1 / (1 + mc.COMPETITORS)  # we + the other buyers after the same item
        self.assertEqual(mc.obtainable(it, pts, None, 1), int(mc.pctl([p[3] for p in pts], 0.25) * 7 * share))
        self.assertLess(mc.obtainable(it, pts, None, 1), 10_000 * 7 * share)  # less than a fair share of a normal week
        it["limit"] = 1
        self.assertEqual(mc.obtainable(it, pts, None, 1), mc.BUY_WINDOWS)  # buy limit binds
        # machines that make things: we must also be able to SELL our share of the output
        self.assertEqual(mc.obtainable({"limit": 100_000}, pts, [(p[0], 1, 1, 0, 50) for p in pts], 1),
                         int(50 * 7 * share))

    def test_fill_plan_prefers_consistency_and_skips_crumbs(self):
        rows = [{"item": "A", "per_week_max": 3_050, "p10": 1_500, "median": 1_600, "buy_at": 7_000, "adj": 1_360, "timing": None},
                {"item": "B", "per_week_max": 40_000, "p10": 1_200, "median": 1_400, "buy_at": 7_000, "adj": 1_060, "timing": None}]
        f = mc.fill_plan(rows, 4_200, 3)
        self.assertEqual([(p["item"], p["qty"]) for p in f["items"]], [("A", 3_000), ("B", 1_200)])
        rows[0]["per_week_max"] = 4_000  # B would only add 200 x 1,200 = 240k a week: not worth a slot
        f = mc.fill_plan(rows, 4_200, 3)
        self.assertEqual([p["item"] for p in f["items"]], ["A"])


class MachineDeskTests(LedgerCase):
    def setUp(self):
        super().setUp()
        mc.DEFAULT_SCHEDULE = {**self._sched, "buy_days": 3.5}  # these tests check the window edge too
        self.clock = dt.datetime(2026, 10, 31, 8, 0).astimezone()  # Saturday morning: buy window open

    def test_outside_window_no_machine_buys(self):
        self.clock = dt.datetime(2026, 10, 28, 9, 0).astimezone()
        self.plan()
        s = desk.load_state()
        self.assertFalse([o for o in s["orders"] if o.get("strategy") == "machine"])

    def test_machine_buy_planned_first_and_paid_from_machine_pot(self):
        out = self.plan()
        self.assertIn("⚙ for the Alchemiser mk. II", out)
        s = desk.load_state()
        mo = [o for o in s["orders"] if o.get("strategy") == "machine"]
        self.assertEqual(len(mo), 1)
        o = mo[0]
        self.assertEqual(o["qty"], 5_000)  # 8,000 on this week's list, capped by the 5,000 buy limit
        self.assertEqual(o["price"], 20_001)
        cash = s["cash"]
        self.assertEqual(desk.reserved_cash(s), sum(desk.remaining(x) * x["price"] for x in s["orders"]
                                                    if x["status"] == "open" and x["side"] == "buy"
                                                    and x.get("strategy") != "machine"))
        n = next(k for k, v in s["plans"][-1]["map"].items() if v == o["id"])
        self.fill(int(n))
        s = desk.load_state()
        self.assertEqual(s["cash"], cash, "machine inputs don't touch flipping cash")
        self.assertNotIn(str(o["item_id"]), {k for k, h in s["holdings"].items() if h["qty"] > 0})
        self.assertFalse([x for x in s["orders"] if x["side"] == "sell" and x["name"] == o["name"]], "no auto-sell")
        w = s["machines"]["week"]
        self.assertEqual((w["bought"]["Speedy whirligig shell"], w["spent"]), (5_000, 5_000 * 20_001))
        # 4 h later: the limit has reset, the rest of the week's list is planned
        self.clock += dt.timedelta(hours=4, minutes=5)
        self.plan()
        s = desk.load_state()
        nxt = [x for x in s["orders"] if x.get("strategy") == "machine" and x["status"] == "open"]
        self.assertEqual([x["qty"] for x in nxt], [3_000])
        self.assertEqual(desk.machine_left(s, s["machines"]["week"], "Speedy whirligig shell"), 3_000)

    def test_pick_the_weeks_item(self):
        self.plan()  # builds this week's list
        (desk.LEDGER / "machines.json").write_text(json.dumps({"machines": {"alch": {
            **FAKE_MACHINE_REPORT["machines"]["alch"],
            "items": [{"item": "Bones", "per_week_max": 6_050, "buy_at": 1_001, "p10": 50, "timing": None}]}}}),
            encoding="utf-8")
        self.run_cmd(desk.cmd_machines, refresh=False, setup=None, pick=["alch=Bones"], loaded=None, bank=None)
        w = desk.load_state()["machines"]["week"]
        self.assertEqual([(x["item"], x["qty"]) for x in w["targets"] if x["kind"] == "alch"], [("Bones", 6_000)])

    def test_keep_machines_capped(self):
        # Saturday 08:00. Both alchemisers (cap 2 x 5,000) were filled at last Tuesday's reset and eat 50/h;
        # 5,200 more sit in the bank. Next refill Tuesday 18:00 is 82 h away.
        self.plan()
        out = self.run_cmd(desk.cmd_machines, refresh=False, setup=None, pick=None,
                           loaded=['alch="Speedy whirligig shell=10000@2026-10-27T18:00"'],
                           bank=['alch="Speedy whirligig shell=5200"'])
        s = desk.load_state()
        w = s["machines"]["week"]
        p = desk.project_loaded(s, w)["alch"]
        self.assertEqual((p["qty"], p["at_refill"], p["idle_h"]), (10_000 - 50 * 86, 1_600, 0))  # Tue 18:00 -> Sat 08:00 = 86 h
        # top back up to 10,000 at the refill: 10,000 - 1,600 inside - 5,200 in the bank = 3,200 to buy
        self.assertEqual(desk.machine_left(s, w, "Speedy whirligig shell"), 3_200)
        self.assertIn("topping up to the 10,000 cap takes 8,400", out)
        # after the next refill they're assumed capped again, a fresh week
        self.clock = dt.datetime(2026, 11, 4, 9, 0).astimezone()
        s = desk.load_state()
        w = desk.machine_week(s, self.clock)
        self.assertEqual(desk.project_loaded(s, w)["alch"]["qty"], 10_000 - 50 * 15)

    def test_machine_items_not_flipped(self):
        self.plan()
        s = desk.load_state()
        flips = [o for o in s["orders"] if o["name"] == "Speedy whirligig shell" and o.get("strategy") != "machine"]
        self.assertFalse(flips)

