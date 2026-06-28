import argparse
import asyncio
import json
import os
import sys
import uuid
from decimal import Decimal
from pathlib import Path

from trading_core import (
    BUY,
    SELL,
    UserInputError,
    as_decimal,
    calculate_order,
    decimal_arg,
    limit_price,
    load_instrument,
    make_strategy_id,
    normalize_owner,
    order_message,
    order_qty,
    order_side,
    price_mode,
    round_to_step,
)

FINISHED = {"Filled", "Cancelled"}


def parse_json_message(message):
    try:
        return json.loads(message.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def parse_trade(data):
    order_id = data.get("orderid", data.get("orderId"))
    trade_id = data.get("tradeid", data.get("tradeId"))
    security = data.get("securityname", data.get("securityName"))
    price = data.get("price")
    qty = data.get("qty")
    if None in (order_id, trade_id, security, price, qty):
        return None

    volume = data.get("volume")
    return {
        "order_id": int(order_id),
        "trade_id": str(trade_id),
        "security": str(security),
        "side": str(data.get("tradeside", data.get("tradeSide", ""))).upper(),
        "price": as_decimal(price),
        "qty": abs(as_decimal(qty)),
        "volume": abs(as_decimal(volume)) if volume is not None else None,
    }


def add_trade(state, trade):
    key = (trade["order_id"], trade["trade_id"])
    if key in state["seen_trades"]:
        return
    state["seen_trades"].add(key)
    state["trades"].append(trade)


def move_early_trades(state):
    if state["order_id"] is None:
        return

    still_unknown = []
    for trade in state["early_trades"]:
        if trade["order_id"] == state["order_id"]:
            add_trade(state, trade)
        else:
            still_unknown.append(trade)
    state["early_trades"] = still_unknown[-1000:]


def result_line(instrument, side, order_id, status, trades):
    filled_lots = sum((trade["qty"] for trade in trades), Decimal("0"))
    filled_value = Decimal("0")
    for trade in trades:
        if trade["volume"] is None:
            filled_value += trade["price"] * trade["qty"] * Decimal(instrument.lot_size)
        else:
            filled_value += trade["volume"]

    if filled_lots == 0:
        status_text = status.get("status") if status else "Unknown"
        return (
            f"Ордер {order_id} ({instrument.security_id}, {side}): статус {status_text}, "
            "ничего не исполнено: 0 лотов на 0.00 RUB, средняя цена 0.00000. Количество сделок: 0"
        )

    avg_price = filled_value / (filled_lots * Decimal(instrument.lot_size))
    lots_text = str(int(filled_lots)) if filled_lots == filled_lots.to_integral_value() else str(filled_lots)
    return (
        f"Ордер {order_id} ({instrument.security_id}, {side}): исполнено {lots_text} лотов "
        f"на {filled_value:.2f} RUB, средняя цена {avg_price:.5f}. "
        f"Количество сделок: {len(trades)}"
    )


def new_runtime_state():
    return {
        "last_price": None,
        "best_bid": None,
        "best_ask": None,
        "order_id": None,
        "status": None,
        "trades": [],
        "early_trades": [],
        "seen_trades": set(),
    }


def apply_tick(state, instrument, data):
    if data.get("securityId") != instrument.ticker or data.get("securityExchange") != instrument.market:
        return False
    if data.get("price") is None:
        return False
    state["last_price"] = as_decimal(data["price"])
    return True


def apply_orderbook(state, instrument, data):
    if data.get("ticker") != instrument.ticker or data.get("market") != instrument.market:
        return False
    bids = data.get("bid_prices") or []
    asks = data.get("ask_prices") or []
    if bids:
        state["best_bid"] = as_decimal(bids[0])
    if asks:
        state["best_ask"] = as_decimal(asks[0])
    return bool(bids or asks)


def apply_status(state, strategy_id, data):
    if data.get("strategyName") != strategy_id:
        return False, False

    state["status"] = data
    order_id = data.get("orderId", data.get("orderid"))
    has_order_id = order_id is not None
    if has_order_id:
        state["order_id"] = int(order_id)
        move_early_trades(state)
    return has_order_id, data.get("status") in FINISHED


def apply_trade(state, instrument, data):
    trade = parse_trade(data)
    if trade is None or trade["security"] != instrument.security_id:
        return False
    if state["order_id"] is None:
        state["early_trades"].append(trade)
        state["early_trades"] = state["early_trades"][-1000:]
        return True
    if trade["order_id"] == state["order_id"]:
        add_trade(state, trade)
        return True
    return False


def dry_market(args):
    return {
        "last_price": args.last_price,
        "best_bid": args.best_bid,
        "best_ask": args.best_ask,
    }


def run_dry(args):
    instrument = load_instrument(args.ticker, args.market, args.data_dir)
    mode, side, price, qty, strategy_id, message = calculate_order(args, instrument, dry_market(args))
    print("DRY-RUN: заявка не отправлена в RabbitMQ")
    print(f"{instrument.security_id}, {side}, mode={mode}, price={price}, qty={qty}")
    print(json.dumps(message, ensure_ascii=False, indent=2))
    return 0


def has_market_data(mode, side, state):
    if mode == "price":
        return True
    if mode == "slippage":
        return state["last_price"] is not None
    if side == BUY:
        return state["best_ask"] is not None
    return state["best_bid"] is not None


def raise_callback_error(callback_error):
    error = callback_error.get("error")
    if error is not None:
        raise RuntimeError(f"ошибка обработки сообщения RabbitMQ: {error}") from error


async def wait_market_data(mode, side, state, event, timeout, callback_error=None):
    callback_error = callback_error or {}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        raise_callback_error(callback_error)
        event.clear()
        if has_market_data(mode, side, state):
            return
        left = deadline - loop.time()
        if left <= 0:
            raise TimeoutError("не дождались нужных рыночных данных")
        await asyncio.wait_for(event.wait(), timeout=left)


async def declare_queue(channel, owner, ticker, kind):
    name = f"{owner}_{ticker.lower()}_{kind}_{uuid.uuid4().hex[:8]}"
    return await channel.declare_queue(
        name,
        durable=False,
        exclusive=True,
        auto_delete=True,
        arguments={"x-max-length": 1000, "x-overflow": "drop-head"},
    )


async def run_live(args, aio_pika_module=None):
    if not args.rabbit_url:
        raise UserInputError("для --live задайте RABBITMQ_URL или --rabbit-url")
    if aio_pika_module is None:
        try:
            import aio_pika as aio_pika_module
        except ImportError as error:
            raise RuntimeError("для live-режима нужно установить зависимости из requirements.txt") from error
    aio_pika = aio_pika_module

    instrument = load_instrument(args.ticker, args.market, args.data_dir)
    side = order_side(args.qty, args.volume)
    mode = price_mode(args)
    owner = normalize_owner(args.owner)
    strategy_id = make_strategy_id(owner, instrument.ticker)

    state = new_runtime_state()
    market_event = asyncio.Event()
    order_id_event = asyncio.Event()
    final_status_event = asyncio.Event()
    callback_error = {"error": None}
    queues = []
    consumer_tags = []

    def remember_callback_error(error):
        if callback_error["error"] is None:
            callback_error["error"] = error
        market_event.set()
        order_id_event.set()
        final_status_event.set()

    async def on_tick(message):
        try:
            async with message.process(requeue=False):
                data = parse_json_message(message)
                if data and apply_tick(state, instrument, data):
                    market_event.set()
        except Exception as error:
            remember_callback_error(error)

    async def on_orderbook(message):
        try:
            async with message.process(requeue=False):
                data = parse_json_message(message)
                if data and apply_orderbook(state, instrument, data):
                    market_event.set()
        except Exception as error:
            remember_callback_error(error)

    async def on_status(message):
        try:
            async with message.process(requeue=False):
                data = parse_json_message(message)
                if not data:
                    return
                has_order_id, is_finished = apply_status(state, strategy_id, data)
                if has_order_id:
                    order_id_event.set()
                if is_finished:
                    final_status_event.set()
        except Exception as error:
            remember_callback_error(error)

    async def on_trade(message):
        try:
            async with message.process(requeue=False):
                data = parse_json_message(message)
                if data:
                    apply_trade(state, instrument, data)
        except Exception as error:
            remember_callback_error(error)

    connection = await aio_pika.connect_robust(args.rabbit_url)
    try:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=100)

        orders_exchange = await channel.get_exchange("sandbox.orders", ensure=False)
        status_exchange = await channel.get_exchange("sandbox.order.status", ensure=False)
        trades_exchange = await channel.get_exchange("sandbox.trades", ensure=False)
        ticks_exchange = await channel.get_exchange("marketdata.ticks.alor", ensure=False)
        books_exchange = await channel.get_exchange("marketdata.orderbooks.alor", ensure=False)

        status_queue = await declare_queue(channel, owner, instrument.ticker, "status")
        trades_queue = await declare_queue(channel, owner, instrument.ticker, "trades")
        ticks_queue = await declare_queue(channel, owner, instrument.ticker, "ticks")
        books_queue = await declare_queue(channel, owner, instrument.ticker, "books")
        queues = [status_queue, trades_queue, ticks_queue, books_queue]

        await status_queue.bind(status_exchange, routing_key="sandbox.status")
        await trades_queue.bind(trades_exchange)
        await ticks_queue.bind(ticks_exchange)
        await books_queue.bind(books_exchange)

        consumer_tags.append((status_queue, await status_queue.consume(on_status)))
        consumer_tags.append((trades_queue, await trades_queue.consume(on_trade)))
        consumer_tags.append((ticks_queue, await ticks_queue.consume(on_tick)))
        consumer_tags.append((books_queue, await books_queue.consume(on_orderbook)))

        await wait_market_data(
            mode,
            side,
            state,
            market_event,
            args.startup_timeout,
            callback_error,
        )
        _, side, price, qty, _, message = calculate_order(args, instrument, state, strategy_id=strategy_id)
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        await orders_exchange.publish(
            aio_pika.Message(body=body, content_type="application/json"),
            routing_key="locko.place",
        )

        print(f"Отправлена заявка {strategy_id}: {instrument.security_id}, {side}, {qty} лотов по {price}")
        await asyncio.wait_for(order_id_event.wait(), timeout=args.timeout)
        raise_callback_error(callback_error)
        await asyncio.wait_for(final_status_event.wait(), timeout=args.timeout)
        raise_callback_error(callback_error)
        if args.trade_grace > 0:
            await asyncio.sleep(args.trade_grace)

        print(result_line(instrument, side, state["order_id"], state["status"], state["trades"]))
        return 0
    finally:
        for queue, tag in consumer_tags:
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
    parser.add_argument("--best-quote", "--best_quote", action="store_true", help="лучший ask/bid из стакана")
    parser.add_argument("--owner", default=os.getenv("VANILLA_OWNER", "serge"))
    parser.add_argument("--rabbit-url", default=os.getenv("RABBITMQ_URL"))
    parser.add_argument("--portfolio", default=os.getenv("VANILLA_PORTFOLIO", "M01+00000000"))
    parser.add_argument("--client-code", default=os.getenv("VANILLA_CLIENT_CODE", "MIPT"))
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--startup-timeout", type=float, default=120.0)
    parser.add_argument("--trade-grace", type=float, default=1.0)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent)
    run_mode = parser.add_mutually_exclusive_group()
    run_mode.add_argument("--live", action="store_true", help="подключиться к RabbitMQ и отправить заявку")
    run_mode.add_argument("--dry-run", action="store_true", help="только показать заявку; режим по умолчанию")
    parser.add_argument("--last-price", type=decimal_arg)
    parser.add_argument("--best-bid", type=decimal_arg)
    parser.add_argument("--best-ask", type=decimal_arg)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.live:
            return asyncio.run(run_live(args))
        return run_dry(args)
    except (UserInputError, RuntimeError, TimeoutError, asyncio.TimeoutError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
