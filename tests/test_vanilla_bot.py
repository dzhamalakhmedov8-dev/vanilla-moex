import argparse
import asyncio
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vanilla_bot import (  # noqa: E402
    BUY,
    SELL,
    add_trade,
    apply_orderbook,
    apply_status,
    apply_tick,
    apply_trade,
    build_parser,
    has_market_data,
    limit_price,
    load_instrument,
    new_runtime_state,
    order_message,
    order_qty,
    order_side,
    parse_trade,
    parse_json_message,
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
    def __init__(self, name, declare_kwargs=None):
        self.name = name
        self.declare_kwargs = declare_kwargs or {}
        self.binds = []
        self.callback = None
        self.deleted = False
        self.cancelled = []

    async def bind(self, exchange, routing_key=None):
        self.binds.append((exchange.name, routing_key))
        return None

    async def consume(self, callback):
        self.callback = callback
        return f"consumer-{self.name}"

    async def cancel(self, tag):
        self.cancelled.append(tag)
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
        if self.channel.fail_publish:
            raise RuntimeError("publish failed")

        order = self.channel.published[-1][0]
        await self.channel.after_publish(order)


class FakeChannel:
    def __init__(self, scenario="filled_early", fail_publish=False):
        self.scenario = scenario
        self.fail_publish = fail_publish
        self.queues = []
        self.published = []
        self.exchanges_requested = []
        self.qos = None

    async def set_qos(self, prefetch_count):
        self.qos = prefetch_count
        return None

    async def get_exchange(self, name, ensure=False):
        self.exchanges_requested.append((name, ensure))
        return FakeExchange(name, self)

    async def declare_queue(self, name, **kwargs):
        queue = FakeQueue(name, kwargs)
        self.queues.append(queue)
        return queue

    def queue(self, kind):
        return next(queue for queue in self.queues if f"_{kind}_" in queue.name)

    async def send_status(self, order, status="Filled", order_id=42, strategy_id=None):
        data = {
            "strategyName": strategy_id or order["OrderStrategyId"],
            "orderId": order_id,
            "status": status,
        }
        await self.queue("status").callback(FakeMessage(json.dumps(data).encode()))

    async def send_trade(self, order, trade_id=1001, order_id=42, qty=None, security=None):
        qty = abs(order["OrderQty"]) if qty is None else qty
        trade = {
            "orderid": order_id,
            "tradeid": trade_id,
            "tradeside": "BUY" if order["OrderQty"] > 0 else "SELL",
            "securityname": security or order["OrderSecurityId"],
            "price": order["OrderPrice"],
            "qty": qty,
            "volume": Decimal(str(qty)) * Decimal(str(order["OrderPrice"])),
        }
        await self.queue("trades").callback(FakeMessage(json.dumps(trade, default=str).encode()))

    async def after_publish(self, order):
        if self.scenario == "no_events":
            return
        if self.scenario == "bad_status":
            await self.send_status(order, order_id="wrong")
            return
        if self.scenario == "cancelled":
            await self.send_status(order, status="Cancelled")
            return
        if self.scenario == "multi_partial":
            await self.send_status(order, strategy_id="foreign_strategy")
            await self.send_trade(order, trade_id=9001, order_id=99)
            await self.send_trade(order, trade_id=1001, qty="1")
            await self.send_trade(order, trade_id=1001, qty="1")
            await self.send_trade(order, trade_id=1002, qty="0.5")
            await self.send_status(order, status="Cancelled")
            return
        if self.scenario == "late_trade":
            await self.send_status(order)
            asyncio.create_task(self.send_late_trade(order))
            return

        await self.send_trade(order)
        await self.send_status(order)

    async def send_late_trade(self, order):
        await asyncio.sleep(0.01)
        await self.send_trade(order)


class FakeConnection:
    def __init__(self, scenario="filled_early", fail_publish=False):
        self.test_channel = FakeChannel(scenario=scenario, fail_publish=fail_publish)
        self.closed = False

    async def channel(self):
        return self.test_channel

    async def close(self):
        self.closed = True


class FakeAioPika:
    Message = FakeMessage

    def __init__(self, scenario="filled_early", fail_publish=False):
        self.connection = FakeConnection(scenario=scenario, fail_publish=fail_publish)

    async def connect_robust(self, url):
        return self.connection


class VanillaBotTests(unittest.TestCase):
    def setUp(self):
        self.sber = load_instrument("SBER", "TQBR", ROOT)
        self.cny = load_instrument("CNYRUB_TOM", "CETS", ROOT)

    def live_args(self, extra=None):
        args = [
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
        if extra:
            args.extend(extra)
        return build_parser().parse_args(args)

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
        message = order_message("dakhmedov_SBER_1", self.sber, Decimal("303.15"), 10, "P", "C")
        self.assertEqual(message["OrderStrategyId"], "dakhmedov_SBER_1")
        self.assertEqual(message["OrderSecurityId"], "SBER@TQBR")
        self.assertEqual(message["OrderType"], "LMT")
        self.assertEqual(message["OrderPrice"], 303.15)
        self.assertEqual(message["OrderQty"], 10)

    def test_price_requires_market_data(self):
        with self.assertRaises(UserInputError):
            limit_price("slippage", BUY, self.sber, slippage=Decimal("1"))
        with self.assertRaises(UserInputError):
            limit_price("best_quote", BUY, self.sber, best_bid=Decimal("300"))
        with self.assertRaises(UserInputError):
            limit_price("best_quote", SELL, self.sber, best_ask=Decimal("301"))

    def test_bad_json_message_is_ignored(self):
        self.assertIsNone(parse_json_message(FakeMessage(b"{bad json")))
        self.assertIsNone(parse_json_message(FakeMessage(b"\xff")))

    def test_empty_market_data_is_not_ready(self):
        state = new_runtime_state()
        self.assertFalse(
            apply_tick(
                state,
                self.sber,
                {
                    "securityId": "SBER",
                    "securityExchange": "TQBR",
                },
            )
        )
        self.assertFalse(has_market_data("slippage", BUY, state))

        self.assertFalse(
            apply_orderbook(
                state,
                self.sber,
                {
                    "ticker": "SBER",
                    "market": "TQBR",
                    "bid_prices": [],
                    "ask_prices": [],
                },
            )
        )
        self.assertFalse(has_market_data("best_quote", BUY, state))
        self.assertFalse(has_market_data("best_quote", SELL, state))

    def test_apply_status_and_trade_ignore_foreign_messages_and_deduplicate(self):
        state = new_runtime_state()
        strategy_id = "dakhmedov_SBER_1"
        foreign_status = {"strategyName": "other", "orderId": 42, "status": "Filled"}
        self.assertEqual(apply_status(state, strategy_id, foreign_status), (False, False))
        self.assertIsNone(state["order_id"])

        own_status = {"strategyName": strategy_id, "orderId": 42, "status": "Submitted"}
        self.assertEqual(apply_status(state, strategy_id, own_status), (True, False))

        foreign_trade = {
            "orderid": 99,
            "tradeid": 1,
            "securityname": "SBER@TQBR",
            "price": 300,
            "qty": 1,
        }
        self.assertFalse(apply_trade(state, self.sber, foreign_trade))
        self.assertEqual(state["trades"], [])

        own_trade = {
            "orderid": 42,
            "tradeid": 2,
            "securityname": "SBER@TQBR",
            "price": 300,
            "qty": 1,
        }
        self.assertTrue(apply_trade(state, self.sber, own_trade))
        self.assertTrue(apply_trade(state, self.sber, own_trade))
        self.assertEqual(len(state["trades"]), 1)
        self.assertEqual(state["trades"][0]["qty"], Decimal("1"))

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
        self.assertEqual(args.owner, "dakhmedov")
        args.live = True
        args.rabbit_url = None
        with self.assertRaises(UserInputError):
            asyncio.run(run_live(args, FakeAioPika()))

    def test_live_flow_collects_early_trade_and_cleans_up(self):
        args = self.live_args()
        fake = FakeAioPika()
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = asyncio.run(run_live(args, fake))

        self.assertEqual(code, 0)
        self.assertEqual(fake.connection.test_channel.published[0][1], "locko.place")
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn("исполнено 2 лотов", output.getvalue())
        self.assertIn("Отправлена заявка", errors.getvalue())
        self.assertTrue(fake.connection.closed)
        self.assertTrue(all(queue.deleted for queue in fake.connection.test_channel.queues))

    def test_live_queue_arguments_and_bindings(self):
        args = self.live_args(["--owner", "dakhmedov"])
        fake = FakeAioPika()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            asyncio.run(run_live(args, fake))

        channel = fake.connection.test_channel
        self.assertEqual(channel.qos, 100)
        self.assertEqual(
            channel.exchanges_requested,
            [
                ("sandbox.orders", False),
                ("sandbox.order.status", False),
                ("sandbox.trades", False),
                ("marketdata.ticks.alor", False),
                ("marketdata.orderbooks.alor", False),
            ],
        )
        self.assertEqual(len(channel.queues), 4)
        expected_binds = {
            "status": [("sandbox.order.status", "sandbox.status")],
            "trades": [("sandbox.trades", None)],
            "ticks": [("marketdata.ticks.alor", None)],
            "books": [("marketdata.orderbooks.alor", None)],
        }
        for kind, binds in expected_binds.items():
            queue = channel.queue(kind)
            self.assertRegex(queue.name, rf"^dakhmedov_sber_{kind}_[0-9a-f]{{8}}$")
            self.assertEqual(queue.declare_kwargs["durable"], False)
            self.assertEqual(queue.declare_kwargs["exclusive"], True)
            self.assertEqual(queue.declare_kwargs["auto_delete"], True)
            self.assertEqual(queue.declare_kwargs["arguments"], {"x-max-length": 1000, "x-overflow": "drop-head"})
            self.assertEqual(queue.binds, binds)
            self.assertTrue(queue.cancelled)
            self.assertTrue(queue.deleted)

    def test_live_cancelled_without_trades_prints_zero_result(self):
        args = self.live_args()
        fake = FakeAioPika(scenario="cancelled")
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = asyncio.run(run_live(args, fake))

        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("статус Cancelled", text)
        self.assertIn("0 лотов на 0.00 RUB", text)
        self.assertIn("Количество сделок: 0", text)

    def test_live_ignores_foreign_messages_and_duplicate_trades(self):
        args = self.live_args()
        fake = FakeAioPika(scenario="multi_partial")
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = asyncio.run(run_live(args, fake))

        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("исполнено 1.5 лотов", text)
        self.assertIn("450.00 RUB", text)
        self.assertIn("Количество сделок: 2", text)

    def test_live_collects_trade_arriving_after_final_status(self):
        args = self.live_args(["--trade-grace", "0.03", "--trade-max-wait", "0.2"])
        fake = FakeAioPika(scenario="late_trade")
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = asyncio.run(run_live(args, fake))

        self.assertEqual(code, 0)
        self.assertIn("исполнено 2 лотов", output.getvalue())

    def test_live_market_data_timeout_cleans_up(self):
        args = self.live_args(
            [
                "--best-quote",
                "--startup-timeout",
                "0.01",
            ]
        )
        args.price = None
        fake = FakeAioPika(scenario="no_events")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(TimeoutError):
                asyncio.run(run_live(args, fake))
        self.assertTrue(fake.connection.closed)
        self.assertTrue(all(queue.deleted for queue in fake.connection.test_channel.queues))

    def test_live_order_id_timeout_cleans_up(self):
        args = self.live_args(["--timeout", "0.01"])
        fake = FakeAioPika(scenario="no_events")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(TimeoutError):
                asyncio.run(run_live(args, fake))
        self.assertTrue(fake.connection.closed)
        self.assertTrue(all(queue.deleted for queue in fake.connection.test_channel.queues))

    def test_live_publish_error_cleans_up(self):
        args = self.live_args()
        fake = FakeAioPika(fail_publish=True)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "publish failed"):
                asyncio.run(run_live(args, fake))
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
        fake = FakeAioPika(scenario="bad_status")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "ошибка обработки сообщения RabbitMQ"):
                asyncio.run(run_live(args, fake))
        self.assertTrue(fake.connection.closed)
        self.assertTrue(all(queue.deleted for queue in fake.connection.test_channel.queues))


if __name__ == "__main__":
    unittest.main()
