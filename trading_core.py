import argparse
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path


BUY = "BUY"
SELL = "SELL"

INSTRUMENT_FILES = {
    ("SBER", "TQBR"): "SBER.json",
    ("CNYRUB_TOM", "CETS"): "CNYRUB_TOM.json",
}


class UserInputError(ValueError):
    pass


@dataclass(frozen=True)
class Instrument:
    ticker: str
    market: str
    lot_size: int
    min_step: Decimal

    @property
    def security_id(self):
        return f"{self.ticker}@{self.market}"


def as_decimal(value):
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def decimal_arg(text):
    try:
        return Decimal(text)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(f"не число: {text}") from error


def load_instrument(ticker, market, data_dir=None):
    ticker = ticker.upper()
    market = market.upper()
    filename = INSTRUMENT_FILES.get((ticker, market))
    if filename is None:
        raise UserInputError("поддерживаются только SBER TQBR и CNYRUB_TOM CETS")

    base = Path(data_dir) if data_dir else Path(__file__).resolve().parent
    path = base / filename
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise UserInputError(f"не удалось прочитать {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise UserInputError(f"в {path.name} повреждён JSON: {error}") from error

    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise UserInputError(f"{path.name} должен содержать непустой список инструментов")

    info = rows[0]
    missing = {"SECID", "BOARDID", "LOTSIZE", "MINSTEP"} - set(info)
    if missing:
        raise UserInputError(f"в {path.name} нет полей: {', '.join(sorted(missing))}")

    try:
        lot_size = int(info["LOTSIZE"])
        min_step = as_decimal(info["MINSTEP"])
    except (TypeError, ValueError, InvalidOperation) as error:
        raise UserInputError(f"в {path.name} неверные LOTSIZE или MINSTEP") from error

    if info["SECID"] != ticker or info["BOARDID"] != market:
        raise UserInputError(f"{path.name} описывает другой инструмент")
    if lot_size <= 0 or min_step <= 0:
        raise UserInputError(f"в {path.name} LOTSIZE и MINSTEP должны быть положительными")

    return Instrument(ticker=ticker, market=market, lot_size=lot_size, min_step=min_step)


def normalize_owner(owner):
    value = re.sub(r"[^a-z0-9_]+", "", owner.lower())
    if len(value) < 2:
        raise UserInputError("owner должен быть латиницей, например dakhmedov или ipopov")
    return value


def make_strategy_id(owner, ticker):
    owner = normalize_owner(owner)
    now = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{owner}_{ticker}_{now}_{uuid.uuid4().hex[:6]}"


def order_side(qty, volume):
    if qty is not None:
        if qty == 0:
            raise UserInputError("qty не может быть нулём")
        return BUY if qty > 0 else SELL

    if volume is None:
        raise UserInputError("нужно передать qty или volume")
    if volume == 0:
        raise UserInputError("volume не может быть нулём")
    return BUY if volume > 0 else SELL


def price_mode(args):
    if args.price is not None:
        if args.price <= 0:
            raise UserInputError("price должен быть положительным")
        return "price"
    if args.slippage is not None:
        if args.slippage < 0:
            raise UserInputError("slippage не может быть отрицательным")
        return "slippage"
    if args.best_quote:
        return "best_quote"
    raise UserInputError("нужно передать price, slippage или best_quote")


def round_to_step(price, step):
    steps = (price / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return steps * step


def limit_price(
    mode,
    side,
    instrument,
    fixed_price=None,
    slippage=None,
    last_price=None,
    best_bid=None,
    best_ask=None,
):
    if mode == "price":
        return fixed_price

    if mode == "slippage":
        if last_price is None:
            raise UserInputError("для slippage ещё нет последней цены")
        k = Decimal("1") + slippage / Decimal("100") if side == BUY else Decimal("1") - slippage / Decimal("100")
        if k <= 0:
            raise UserInputError("slippage даёт неположительную цену")
        return round_to_step(last_price * k, instrument.min_step)

    if mode == "best_quote":
        if side == BUY:
            if best_ask is None:
                raise UserInputError("для покупки нет лучшего ask")
            return best_ask
        if best_bid is None:
            raise UserInputError("для продажи нет лучшего bid")
        return best_bid

    raise UserInputError(f"неизвестный способ цены: {mode}")


def order_qty(qty, volume, side, price, instrument):
    if qty is not None:
        return qty

    one_lot = price * Decimal(instrument.lot_size)
    lots = int((abs(volume) / one_lot).to_integral_value(rounding=ROUND_FLOOR))
    if lots < 1:
        raise UserInputError("volume слишком маленький даже для одного лота")
    return lots if side == BUY else -lots


def order_message(strategy_id, instrument, price, qty, portfolio, client_code):
    return {
        "OrderStrategyId": strategy_id,
        "OrderSecurityId": instrument.security_id,
        "OrderType": "LMT",
        "OrderPrice": float(price),
        "OrderQty": qty,
        "OrderPortfolio": portfolio,
        "OrderClientCode": client_code,
    }


def calculate_order(args, instrument, market, strategy_id=None):
    side = order_side(args.qty, args.volume)
    mode = price_mode(args)
    price = limit_price(
        mode,
        side,
        instrument,
        fixed_price=args.price,
        slippage=args.slippage,
        last_price=market.get("last_price"),
        best_bid=market.get("best_bid"),
        best_ask=market.get("best_ask"),
    )
    qty = order_qty(args.qty, args.volume, side, price, instrument)
    strategy_id = strategy_id or make_strategy_id(args.owner, instrument.ticker)
    message = order_message(strategy_id, instrument, price, qty, args.portfolio, args.client_code)
    return mode, side, price, qty, strategy_id, message
