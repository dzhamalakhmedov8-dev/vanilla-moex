import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path


BUY = "BUY"
SELL = "SELL"
FINISHED = {"Filled", "Cancelled"}

QUEUE_MAX_LEN = 1000
TRADE_WAIT_SECONDS = 1.0
TIMEOUT = 600.0
STARTUP_TIMEOUT = 120.0

INSTRUMENT_FILES = {
    ("SBER", "TQBR"): "SBER.json",
    ("CNYRUB_TOM", "CETS"): "CNYRUB_TOM.json",
}


def decimal_arg(text):
    try:
        return Decimal(text)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(f"не число: {text}") from error


def dec(value):
    return Decimal(str(value))


def read_instrument(ticker, market):
    ticker = ticker.upper()
    market = market.upper()
    filename = INSTRUMENT_FILES.get((ticker, market))
    if filename is None:
        raise ValueError("поддерживаются только SBER TQBR и CNYRUB_TOM CETS")

    script_dir = Path(__file__).resolve().parent
    instrument_path = script_dir / filename
    instrument_text = instrument_path.read_text(encoding="utf-8")
    row = json.loads(instrument_text)[0]
    file_ticker = row.get("SECID")
    file_market = row.get("BOARDID")
    if (file_ticker is not None and str(file_ticker).upper() != ticker) or (
        file_market is not None and str(file_market).upper() != market
    ):
        raise ValueError("json-файл описывает другой инструмент")

    return {
        "ticker": ticker,
        "market": market,
        "security": f"{ticker}@{market}",
        "lot_size": int(row["LOTSIZE"]),
        "min_step": dec(row["MINSTEP"]),
    }


def owner_code(owner):
    owner = re.sub(r"[^a-z0-9_]+", "", owner.lower())
    if len(owner) < 2:
        raise ValueError("owner должен быть латиницей, например dakhmedov")
    return owner


def make_strategy_id(owner, ticker):
    now = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{owner_code(owner)}_{ticker}_{now}_{uuid.uuid4().hex[:6]}"


def choose_side(args):
    if args.qty is not None:
        if args.qty == 0:
            raise ValueError("qty не может быть нулём")
        return BUY if args.qty > 0 else SELL
    if args.volume is None:
        raise ValueError("нужно передать qty или volume")
    if args.volume == 0:
        raise ValueError("volume не может быть нулём")
    return BUY if args.volume > 0 else SELL


def choose_price_mode(args):
    if args.price is not None:
        if args.price <= 0:
            raise ValueError("price должен быть положительным")
        return "price"
    if args.slippage is not None:
        if args.slippage < 0:
            raise ValueError("slippage не может быть отрицательным")
        return "slippage"
    if args.best_quote:
        return "best_quote"
    raise ValueError("нужно передать price, slippage или best_quote")


def round_to_step(price, step):
    steps = (price / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return steps * step


def market_ready(mode, side, state):
    if mode == "price":
        return True
    if mode == "slippage":
        return state["last_price"] is not None
    if side == BUY:
        return state["best_ask"] is not None
    return state["best_bid"] is not None


def parse_json(message):
    try:
        return json.loads(message.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def parse_trade(data):
    order_id = data.get("orderid")
    trade_id = data.get("tradeid")
    security = data.get("securityname")
    if None in (order_id, trade_id, security, data.get("price"), data.get("qty")):
        return None

    volume = data.get("volume")
    return {
        "order_id": int(order_id),
        "trade_id": str(trade_id),
        "security": str(security),
        "price": dec(data["price"]),
        "qty": abs(dec(data["qty"])),
        "volume": abs(dec(volume)) if volume is not None else None,
    }


def add_trade(state, trade):
    key = (trade["order_id"], trade["trade_id"])
    if key in state["seen_trades"]:
        return
    state["seen_trades"].add(key)
    state["trades"].append(trade)


def move_early_trades(state):
    # Трейды могут прийти раньше статуса с orderId: сообщения идут через разные очереди.
    # Такие сделки временно сохраняются и разбираются после появления orderId.
    if state["order_id"] is None:
        return
    rest = []
    for trade in state["early_trades"]:
        if trade["order_id"] == state["order_id"]:
            add_trade(state, trade)
        else:
            rest.append(trade)
    state["early_trades"] = rest[-QUEUE_MAX_LEN:]


def result_line(instrument, side, order_id, status, trades):
    lots = sum((trade["qty"] for trade in trades), Decimal("0"))
    value = Decimal("0")
    for trade in trades:
        if trade["volume"] is None:
            value += trade["price"] * trade["qty"] * Decimal(instrument["lot_size"])
        else:
            value += trade["volume"]

    if lots == 0:
        status_text = status.get("status") if status else "Unknown"
        return (
            f"Ордер {order_id} ({instrument['security']}, {side}): статус {status_text}, "
            "ничего не исполнено: 0 лотов на 0.00 RUB, средняя цена 0.00000. Количество сделок: 0"
        )

    avg_price = value / (lots * Decimal(instrument["lot_size"]))
    lots_text = str(int(lots)) if lots == lots.to_integral_value() else str(lots)
    return (
        f"Ордер {order_id} ({instrument['security']}, {side}): исполнено {lots_text} лотов "
        f"на {value:.2f} RUB, средняя цена {avg_price:.5f}. "
        f"Количество сделок: {len(trades)}"
    )


async def wait_market_data(mode, side, state, event, timeout, error_box):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not market_ready(mode, side, state):
        if error_box["error"] is not None:
            raise RuntimeError(f"ошибка обработки сообщения RabbitMQ: {error_box['error']}")
        left = deadline - loop.time()
        if left <= 0:
            raise TimeoutError("не дождались нужных рыночных данных")
        event.clear()
        await asyncio.wait_for(event.wait(), timeout=left)


async def make_queue(channel, owner, ticker, kind):
    name = f"{owner}_{ticker.lower()}_{kind}_{uuid.uuid4().hex[:8]}"
    return await channel.declare_queue(
        name,
        durable=False,
        exclusive=True,
        auto_delete=True,
        arguments={"x-max-length": QUEUE_MAX_LEN, "x-overflow": "drop-head"},
    )


async def run(args):
    if not args.rabbit_url:
        raise ValueError("задайте RABBITMQ_URL или --rabbit-url")

    import aio_pika

    instrument = read_instrument(args.ticker, args.market)
    side = choose_side(args)
    mode = choose_price_mode(args)
    owner = owner_code(args.owner)
    order_strategy_id = make_strategy_id(owner, instrument["ticker"])

    state = {
        "last_price": None,
        "best_bid": None,
        "best_ask": None,
        "order_id": None,
        "status": None,
        "trades": [],
        "early_trades": [],
        "seen_trades": set(),
    }

    market_event = asyncio.Event()
    order_id_event = asyncio.Event()
    final_status_event = asyncio.Event()

    # Ошибка из consumer сохраняется здесь, чтобы основное ожидание не висело до таймаута.
    error_box = {"error": None}
    queues = []
    consumers = []

    def remember_error(error):
        if error_box["error"] is None:
            error_box["error"] = error
        market_event.set()
        order_id_event.set()
        final_status_event.set()

    async def on_tick(message):
        try:
            async with message.process(requeue=False):
                data = parse_json(message)
                if not data:
                    return
                if data.get("securityId") == instrument["ticker"] and data.get("securityExchange") == instrument["market"]:
                    if data.get("price") is not None:
                        state["last_price"] = dec(data["price"])
                        market_event.set()
        except Exception as error:
            remember_error(error)

    async def on_orderbook(message):
        try:
            async with message.process(requeue=False):
                data = parse_json(message)
                if not data:
                    return
                if data.get("ticker") != instrument["ticker"] or data.get("market") != instrument["market"]:
                    return
                bids = data.get("bid_prices") or []
                asks = data.get("ask_prices") or []
                if bids:
                    state["best_bid"] = dec(bids[0])
                if asks:
                    state["best_ask"] = dec(asks[0])
                if bids or asks:
                    market_event.set()
        except Exception as error:
            remember_error(error)

    async def on_status(message):
        try:
            async with message.process(requeue=False):
                data = parse_json(message)
                if not data or data.get("strategyName") != order_strategy_id:
                    return
                state["status"] = data
                order_id = data.get("orderId")
                if order_id is not None:
                    state["order_id"] = int(order_id)
                    move_early_trades(state)
                    order_id_event.set()
                if data.get("status") in FINISHED:
                    final_status_event.set()
        except Exception as error:
            remember_error(error)

    async def on_trade(message):
        try:
            async with message.process(requeue=False):
                data = parse_json(message)
                trade = parse_trade(data) if data else None
                if trade is None or trade["security"] != instrument["security"]:
                    return
                if state["order_id"] is None:
                    state["early_trades"].append(trade)
                    state["early_trades"] = state["early_trades"][-QUEUE_MAX_LEN:]
                elif trade["order_id"] == state["order_id"]:
                    add_trade(state, trade)
        except Exception as error:
            remember_error(error)

    connection = await aio_pika.connect_robust(args.rabbit_url)
    try:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=100)

        orders_exchange = await channel.get_exchange("sandbox.orders", ensure=False)
        status_exchange = await channel.get_exchange("sandbox.order.status", ensure=False)
        trades_exchange = await channel.get_exchange("sandbox.trades", ensure=False)
        ticks_exchange = await channel.get_exchange("marketdata.ticks.alor", ensure=False)
        books_exchange = await channel.get_exchange("marketdata.orderbooks.alor", ensure=False)

        status_queue = await make_queue(channel, owner, instrument["ticker"], "status")
        trades_queue = await make_queue(channel, owner, instrument["ticker"], "trades")
        ticks_queue = await make_queue(channel, owner, instrument["ticker"], "ticks")
        books_queue = await make_queue(channel, owner, instrument["ticker"], "books")
        queues = [status_queue, trades_queue, ticks_queue, books_queue]

        await status_queue.bind(status_exchange, routing_key="sandbox.status")
        await trades_queue.bind(trades_exchange)
        await ticks_queue.bind(ticks_exchange)
        await books_queue.bind(books_exchange)

        consumers.append((status_queue, await status_queue.consume(on_status)))
        consumers.append((trades_queue, await trades_queue.consume(on_trade)))
        consumers.append((ticks_queue, await ticks_queue.consume(on_tick)))
        consumers.append((books_queue, await books_queue.consume(on_orderbook)))

        await wait_market_data(mode, side, state, market_event, STARTUP_TIMEOUT, error_box)

        if mode == "price":
            price = args.price
        elif mode == "slippage":
            k = Decimal("1") + args.slippage / Decimal("100")
            if side == SELL:
                k = Decimal("1") - args.slippage / Decimal("100")
            if k <= 0:
                raise ValueError("slippage даёт неположительную цену")
            price = round_to_step(state["last_price"] * k, instrument["min_step"])
        elif side == BUY:
            price = state["best_ask"]
        else:
            price = state["best_bid"]

        if args.qty is not None:
            qty = args.qty
        else:
            lot_price = price * Decimal(instrument["lot_size"])
            lots = int((abs(args.volume) / lot_price).to_integral_value(rounding=ROUND_FLOOR))
            if lots < 1:
                raise ValueError("volume слишком маленький даже для одного лота")
            qty = lots if side == BUY else -lots

        order = {
            "OrderStrategyId": order_strategy_id,
            "OrderSecurityId": instrument["security"],
            "OrderType": "LMT",
            "OrderPrice": float(price),
            "OrderQty": qty,
            "OrderPortfolio": args.portfolio,
            "OrderClientCode": args.client_code,
        }
        body = json.dumps(order, ensure_ascii=False).encode("utf-8")
        message = aio_pika.Message(body=body, content_type="application/json")
        await orders_exchange.publish(message, routing_key="locko.place")

        await asyncio.wait_for(order_id_event.wait(), timeout=TIMEOUT)
        if error_box["error"] is not None:
            raise RuntimeError(f"ошибка обработки сообщения RabbitMQ: {error_box['error']}")
        await asyncio.wait_for(final_status_event.wait(), timeout=TIMEOUT)
        if error_box["error"] is not None:
            raise RuntimeError(f"ошибка обработки сообщения RabbitMQ: {error_box['error']}")

        # Финальный статус иногда приходит раньше последней сделки; короткая пауза помогает не потерять трейд.
        await asyncio.sleep(TRADE_WAIT_SECONDS)
        print(result_line(instrument, side, state["order_id"], state["status"], state["trades"]))
        return 0
    finally:
        for queue, tag in consumers:
            try:
                await queue.cancel(tag)
            except Exception:
                pass
        for queue in queues:
            try:
                await queue.delete(if_unused=False, if_empty=False)
            except Exception:
                pass
        await connection.close()


def build_parser():
    parser = argparse.ArgumentParser(description="Vanilla: одна лимитная заявка в песочницу RabbitMQ.")
    parser.add_argument("ticker", help="SBER или CNYRUB_TOM")
    parser.add_argument("market", help="TQBR или CETS")
    parser.add_argument("--qty", type=int, help="лоты: плюс покупка, минус продажа")
    parser.add_argument("--volume", type=decimal_arg, help="сумма в рублях: плюс покупка, минус продажа")
    parser.add_argument("--price", type=decimal_arg, help="фиксированная лимитная цена")
    parser.add_argument("--slippage", type=decimal_arg, help="процент от последней цены")
    parser.add_argument("--best-quote", dest="best_quote", action="store_true", help="лучший ask/bid из стакана")
    parser.add_argument("--owner", default=os.getenv("VANILLA_OWNER", "dakhmedov"))
    parser.add_argument("--rabbit-url", default=os.getenv("RABBITMQ_URL"))
    parser.add_argument("--portfolio", default=os.getenv("VANILLA_PORTFOLIO", "M01+00000000"))
    parser.add_argument("--client-code", default=os.getenv("VANILLA_CLIENT_CODE", "MIPT"))
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except (ValueError, RuntimeError, TimeoutError, asyncio.TimeoutError, OSError, json.JSONDecodeError, KeyError, IndexError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
