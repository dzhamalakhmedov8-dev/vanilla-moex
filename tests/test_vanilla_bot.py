import argparse
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trading_core import UserInputError  # noqa: E402
from vanilla_bot import (  # noqa: E402
    BUY,
    SELL,
    add_trade,
    apply_status,
    apply_trade,
    build_parser,
    limit_price,
    load_instrument,
    new_runtime_state,
    order_message,
    order_qty,
    order_side,
    parse_trade,
    price_mode,
    result_line,
    round_to_step,
)


class VanillaBotTests(unittest.TestCase):
    def setUp(self):
        self.sber = load_instrument("SBER", "TQBR", ROOT)
        self.cny = load_instrument("CNYRUB_TOM", "CETS", ROOT)

    def test_loads_instruments(self):
        self.assertEqual(self.sber.security_id, "SBER@TQBR")
        self.assertEqual(self.sber.lot_size, 1)
        self.assertEqual(self.sber.min_step, Decimal("0.01"))
        self.assertEqual(self.cny.security_id, "CNYRUB_TOM@CETS")
        self.assertEqual(self.cny.lot_size, 1000)

    def test_order_side_uses_qty_first(self):
        self.assertEqual(order_side(5, Decimal("-1000")), BUY)
        self.assertEqual(order_side(-5, Decimal("1000")), SELL)
        self.assertEqual(order_side(None, Decimal("1000")), BUY)
        self.assertEqual(order_side(None, Decimal("-1000")), SELL)
        with self.assertRaises(UserInputError):
            order_side(None, None)

    def test_price_mode_priority(self):
        args = argparse.Namespace(price=Decimal("300"), slippage=Decimal("1"), best_quote=True)
        self.assertEqual(price_mode(args), "price")
        args = argparse.Namespace(price=None, slippage=Decimal("1"), best_quote=True)
        self.assertEqual(price_mode(args), "slippage")
        args = argparse.Namespace(price=None, slippage=None, best_quote=True)
        self.assertEqual(price_mode(args), "best_quote")

    def test_slippage_rounding_and_best_quote(self):
        self.assertEqual(round_to_step(Decimal("10.4612"), Decimal("0.0005")), Decimal("10.4610"))
        self.assertEqual(round_to_step(Decimal("10.4613"), Decimal("0.0005")), Decimal("10.4615"))
        self.assertEqual(
            limit_price("slippage", BUY, self.sber, slippage=Decimal("0.2"), last_price=Decimal("322.30")),
            Decimal("322.94"),
        )
        self.assertEqual(
            limit_price("slippage", SELL, self.sber, slippage=Decimal("0.2"), last_price=Decimal("322.30")),
            Decimal("321.66"),
        )
        self.assertEqual(
            limit_price("best_quote", BUY, self.sber, best_bid=Decimal("322.29"), best_ask=Decimal("322.31")),
            Decimal("322.31"),
        )
        self.assertEqual(
            limit_price("best_quote", SELL, self.sber, best_bid=Decimal("322.29"), best_ask=Decimal("322.31")),
            Decimal("322.29"),
        )

    def test_volume_to_lots(self):
        self.assertEqual(order_qty(None, Decimal("10000"), BUY, Decimal("322.30"), self.sber), 31)
        self.assertEqual(order_qty(None, Decimal("2000000"), BUY, Decimal("10.46"), self.cny), 191)
        self.assertEqual(order_qty(None, Decimal("-2000000"), SELL, Decimal("10.46"), self.cny), -191)

    def test_order_message(self):
        message = order_message("dakhmedov_SBER_1", self.sber, Decimal("303.15"), 10, "P", "C")
        self.assertEqual(
            message,
            {
                "OrderStrategyId": "dakhmedov_SBER_1",
                "OrderSecurityId": "SBER@TQBR",
                "OrderType": "LMT",
                "OrderPrice": 303.15,
                "OrderQty": 10,
                "OrderPortfolio": "P",
                "OrderClientCode": "C",
            },
        )

    def test_early_trade_moves_after_order_id(self):
        state = new_runtime_state()
        trade = {
            "tradeid": 63784234,
            "orderid": 348917234237,
            "tradeside": "SELL",
            "securityname": "CNYRUB_TOM@CETS",
            "price": 11.086,
            "qty": 179.0,
            "volume": 1984394.0,
        }
        self.assertTrue(apply_trade(state, self.cny, trade))
        self.assertEqual(state["trades"], [])
        self.assertEqual(len(state["early_trades"]), 1)

        status = {"strategyName": "dakhmedov_CNY_1", "orderId": 348917234237, "status": "Filled"}
        self.assertEqual(apply_status(state, "dakhmedov_CNY_1", status), (True, True))
        self.assertEqual(len(state["trades"]), 1)

    def test_trade_summary(self):
        trade = parse_trade(
            {
                "tradeid": 63784234,
                "orderid": 348917234237,
                "tradeside": "SELL",
                "securityname": "CNYRUB_TOM@CETS",
                "price": 11.086,
                "qty": 179.0,
                "volume": 1984394.0,
            }
        )
        state = {"seen_trades": set(), "trades": []}
        add_trade(state, trade)
        add_trade(state, trade)
        line = result_line(self.cny, SELL, 348917234237, {"status": "Filled"}, state["trades"])
        self.assertIn("исполнено 179 лотов", line)
        self.assertIn("1984394.00 RUB", line)
        self.assertIn("Количество сделок: 1", line)

    def test_live_is_default_dry_run_is_explicit(self):
        args = build_parser().parse_args(["SBER", "TQBR", "--qty", "1", "--price", "300"])
        self.assertFalse(args.dry_run)
        self.assertEqual(args.owner, "dakhmedov")
        args = build_parser().parse_args(["SBER", "TQBR", "--qty", "1", "--price", "300", "--dry-run"])
        self.assertTrue(args.dry_run)

    def test_invalid_instrument_json_has_readable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "SBER.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(UserInputError, "непустой список"):
                load_instrument("SBER", "TQBR", directory)


if __name__ == "__main__":
    unittest.main()
