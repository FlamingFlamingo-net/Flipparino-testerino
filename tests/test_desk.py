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
import math
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import desk  # noqa: E402
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


class LedgerCase(unittest.TestCase):
    """Points desk.py at a temp ledger and fakes the price APIs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = (desk.LEDGER, desk.STATE, rm.load_market, rm.load_series, rm.load_series30,
                       rm.get_json, desk._item_ids)
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

    def tearDown(self):
        (desk.LEDGER, desk.STATE, rm.load_market, rm.load_series, rm.load_series30,
         rm.get_json, desk._item_ids) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cmd(self, fn, **kw) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(argparse.Namespace(**kw))
        return buf.getvalue()

    def plan(self, **kw):
        args = {"dry_run": False, "review_all": False, "away": None, **kw}
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
