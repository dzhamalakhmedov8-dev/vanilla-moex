import argparse
import asyncio
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vanilla_bot import (  # noqa: E402
    BUY,
    SELL,
    add_trade,
    build_parser,
    limit_price,
    load_instrument,
    order_message,
    order_qty,
    order_side,
    parse_trade,
    price_mode,
    result_line,
    round_to_step,
    run_live,
)
from trading_core import UserInputError  # noqa: E402


class FakeMessage:
    def __init__(self, body, content_type=None):
        self.body = body
        self.content_type = content_type

    def process(self, requeue=False):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeQueue:
    def __init__(self, name):
        self.name = name
        self.callback = None
        self.deleted = False

    async def bind(self, exchange, routing_key=None):
        return None

    async def consume(self, callback):
        self.callback = callback
        return f"consumer-{self.name}"

    async def cancel(self, tag):
        return None

    async def delete(self, if_unused=False, if_empty=False):
        self.deleted = True


class FakeExchange:
    def __init__(self, name, channel):
        self.name = name
        self.channel = channel

    async def publish(self, message, routing_key):
        self.channel.published.append((json.loads(message.body), routing_key))
        if self.name != "sandbox.orders":
            return

        order = self.channel.published[-1][0]
        trade = {
            "orderid": 42,
            "tradeid": 1001,
            "tradeside": "BUY",
            "securityname": order["OrderSecurityId"],
            "price": order["OrderPrice"],
            "qty": abs(order["OrderQty"]),
            "volume": abs(order["OrderQty"]) * order["OrderPrice"],
        }
        status = {
            "strategyName": order["OrderStrategyId"],
            "orderId": "wrong" if self.channel.bad_status else 42,
            "status": "Filled",
        }
        await self.channel.queue("trades").callback(FakeMessage(json.dumps(trade).encode()))
        await self.channel.queue("status").callback(FakeMessage(json.dumps(status).encode()))


class FakeChannel:
    def __init__(self, bad_status=False):
        self.bad_status = bad_status
        self.queues = []
        self.published = []

    async def set_qos(self, prefetch_count):
        return None

    async def get_exchange(self, name, ensure=False):
        return FakeExchange(name, self)

    async def declare_queue(self, name, **kwargs):
        queue = FakeQueue(name)
        self.queues.append(queue)
        return queue

    def queue(self, kind):
        return next(queue for queue in self.queues if f"_{kind}_" in queue.name)


class FakeConnection:
    def __init__(self, bad_status=False):
        self.test_channel = FakeChannel(bad_status=bad_status)
        self.closed = False

    async def channel(self):
        return self.test_channel

    async def close(self):
        self.closed = True


class FakeAioPika:
    Message = FakeMessage

    def __init__(self, bad_status=False):
        self.connection = FakeConnection(bad_status=bad_status)

    async def connect_robust(self, url):
        return self.connection


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

    def test_order_side(self):
        self.assertEqual(order_side(5, Decimal("-1000")), BUY)
        self.assertEqual(order_side(-5, Decimal("1000")), SELL)
        self.assertEqual(order_side(None, Decimal("1000")), BUY)
        self.assertEqual(order_side(None, Decimal("-1000")), SELL)

    def test_price_mode_priority(self):
        args = argparse.Namespace(price=Decimal("300"), slippage=Decimal("1"), best_quote=True)
        self.assertEqual(price_mode(args), "price")
        args = argparse.Namespace(price=None, slippage=Decimal("1"), best_quote=True)
        self.assertEqual(price_mode(args), "slippage")
        args = argparse.Namespace(price=None, slippage=None, best_quote=True)
        self.assertEqual(price_mode(args), "best_quote")

    def test_rounding_and_slippage_price(self):
        self.assertEqual(round_to_step(Decimal("10.4612"), Decimal("0.0005")), Decimal("10.4610"))
        self.assertEqual(round_to_step(Decimal("10.4613"), Decimal("0.0005")), Decimal("10.4615"))

        buy_price = limit_price(
            "slippage",
            BUY,
            self.sber,
            slippage=Decimal("0.2"),
            last_price=Decimal("322.30"),
        )
        sell_price = limit_price(
            "slippage",
            SELL,
            self.sber,
            slippage=Decimal("0.2"),
            last_price=Decimal("322.30"),
        )
        self.assertEqual(buy_price, Decimal("322.94"))
        self.assertEqual(sell_price, Decimal("321.66"))

    def test_best_quote_price(self):
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
        message = order_message("serge_SBER_1", self.sber, Decimal("303.15"), 10, "P", "C")
        self.assertEqual(message["OrderStrategyId"], "serge_SBER_1")
        self.assertEqual(message["OrderSecurityId"], "SBER@TQBR")
        self.assertEqual(message["OrderType"], "LMT")
        self.assertEqual(message["OrderPrice"], 303.15)
        self.assertEqual(message["OrderQty"], 10)

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
        line = result_line(self.cny, SELL, 348917234237, {"status": "Filled"}, state["trades"])
        self.assertIn("исполнено 179 лотов", line)
        self.assertIn("1984394.00 RUB", line)
        self.assertIn("Количество сделок: 1", line)

    def test_dry_run_is_default_and_live_requires_url(self):
        args = build_parser().parse_args(["SBER", "TQBR", "--qty", "1", "--price", "300"])
        self.assertFalse(args.live)
        args.live = True
        args.rabbit_url = None
        with self.assertRaises(UserInputError):
            asyncio.run(run_live(args, FakeAioPika()))

    def test_live_flow_collects_early_trade_and_cleans_up(self):
        args = build_parser().parse_args(
            [
                "SBER",
                "TQBR",
                "--qty",
                "2",
                "--price",
                "300",
                "--live",
                "--rabbit-url",
                "amqp://test",
                "--trade-grace",
                "0",
            ]
        )
        fake = FakeAioPika()
        output = io.StringIO()
        with redirect_stdout(output):
            code = asyncio.run(run_live(args, fake))

        self.assertEqual(code, 0)
        self.assertEqual(fake.connection.test_channel.published[0][1], "locko.place")
        self.assertIn("исполнено 2 лотов", output.getvalue())
        self.assertTrue(fake.connection.closed)
        self.assertTrue(all(queue.deleted for queue in fake.connection.test_channel.queues))

    def test_invalid_instrument_json_has_readable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "SBER.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(UserInputError, "непустой список"):
                load_instrument("SBER", "TQBR", directory)

    def test_callback_error_stops_live_flow_and_cleans_up(self):
        args = build_parser().parse_args(
            [
                "SBER",
                "TQBR",
                "--qty",
                "1",
                "--price",
                "300",
                "--live",
                "--rabbit-url",
                "amqp://test",
                "--trade-grace",
                "0",
            ]
        )
        fake = FakeAioPika(bad_status=True)
        with redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "ошибка обработки сообщения RabbitMQ"):
                asyncio.run(run_live(args, fake))
        self.assertTrue(fake.connection.closed)
        self.assertTrue(all(queue.deleted for queue in fake.connection.test_channel.queues))


if __name__ == "__main__":
    unittest.main()
