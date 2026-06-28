import argparse
import csv
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path

from trading_core import BUY, SELL, Instrument, UserInputError, load_instrument


MOEX_ENDPOINTS = {
    ("SBER", "TQBR"): ("stock", "shares"),
    ("CNYRUB_TOM", "CETS"): ("currency", "selt"),
}


@dataclass(frozen=True)
class Candle:
    begin: str
    end: str
    open: Decimal
    close: Decimal
    high: Decimal
    low: Decimal
    value: Decimal | None
    volume: Decimal | None


@dataclass(frozen=True)
class Fill:
    date: str
    side: str
    price: Decimal
    lots: int
    value: Decimal
    commission: Decimal
    cash_after: Decimal
    position_after: int


@dataclass(frozen=True)
class EquityPoint:
    date: str
    close: Decimal
    cash: Decimal
    position_lots: int
    equity: Decimal


@dataclass(frozen=True)
class BacktestResult:
    summary: dict
    fills: list[Fill]
    equity_curve: list[EquityPoint]
    candles: list[Candle]


def decimal_value(value):
    if value in (None, ""):
        return None
    return Decimal(str(value))


def moex_candles_url(
    instrument: Instrument,
    date_from: str,
    date_till: str,
    interval: int,
    start: int,
) -> str:
    endpoint = MOEX_ENDPOINTS.get((instrument.ticker, instrument.market))
    if endpoint is None:
        raise UserInputError(f"для {instrument.security_id} не настроен MOEX endpoint")
    engine, market = endpoint
    path = (
        f"https://iss.moex.com/iss/engines/{engine}/markets/{market}"
        f"/boards/{instrument.market}/securities/{instrument.ticker}/candles.json"
    )
    query = urllib.parse.urlencode(
        {
            "from": date_from,
            "till": date_till,
            "interval": interval,
            "iss.meta": "off",
            "iss.only": "candles",
            "start": start,
        }
    )
    return f"{path}?{query}"


def moex_json(url, retries=4, timeout=20.0):
    last_error = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "vanilla-backtest/2.0"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except Exception as error:
            last_error = error
            if attempt + 1 == retries:
                break
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"запрос к MOEX завершился ошибкой: {last_error}") from last_error


def parse_candles(raw):
    table = raw.get("candles", {})
    columns = table.get("columns", [])
    rows = table.get("data", [])
    index = {name: pos for pos, name in enumerate(columns)}
    required = {"open", "close", "high", "low", "begin", "end"}
    missing = required - set(index)
    if missing:
        raise RuntimeError(f"в ответе MOEX нет полей: {', '.join(sorted(missing))}")

    candles = []
    for row_number, row in enumerate(rows, start=1):
        try:
            candles.append(
                Candle(
                    begin=str(row[index["begin"]]),
                    end=str(row[index["end"]]),
                    open=Decimal(str(row[index["open"]])),
                    close=Decimal(str(row[index["close"]])),
                    high=Decimal(str(row[index["high"]])),
                    low=Decimal(str(row[index["low"]])),
                    value=decimal_value(row[index["value"]]) if "value" in index else None,
                    volume=decimal_value(row[index["volume"]]) if "volume" in index else None,
                )
            )
        except (IndexError, InvalidOperation, TypeError) as error:
            raise RuntimeError(f"повреждена свеча MOEX в строке {row_number}") from error
    return candles


def download_candles(
    instrument: Instrument,
    date_from: str,
    date_till: str,
    interval: int = 24,
) -> list[Candle]:
    all_candles = []
    start = 0
    while True:
        url = moex_candles_url(instrument, date_from, date_till, interval, start)
        chunk = parse_candles(moex_json(url))
        if not chunk:
            break
        all_candles.extend(chunk)
        start += len(chunk)
        if len(chunk) < 500:
            break
    return all_candles


def load_candles_csv(path: Path) -> list[Candle]:
    try:
        with path.open(newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            required = {"begin", "end", "open", "close", "high", "low"}
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise UserInputError(f"в {path.name} нет колонок: {', '.join(sorted(missing))}")

            candles = []
            for row_number, row in enumerate(reader, start=2):
                try:
                    candles.append(
                        Candle(
                            begin=row["begin"],
                            end=row["end"],
                            open=Decimal(row["open"]),
                            close=Decimal(row["close"]),
                            high=Decimal(row["high"]),
                            low=Decimal(row["low"]),
                            value=decimal_value(row.get("value")),
                            volume=decimal_value(row.get("volume")),
                        )
                    )
                except (InvalidOperation, TypeError) as error:
                    raise UserInputError(f"в {path.name} неверная строка {row_number}") from error
            return candles
    except OSError as error:
        raise UserInputError(f"не удалось прочитать {path}: {error}") from error


def validate_dates(date_from: str, date_till: str):
    try:
        start = datetime.strptime(date_from, "%Y-%m-%d").date()
        finish = datetime.strptime(date_till, "%Y-%m-%d").date()
    except ValueError as error:
        raise UserInputError("даты должны иметь формат YYYY-MM-DD") from error
    if start > finish:
        raise UserInputError("дата --from не может быть позже --till")


def validate_candles(candles: list[Candle]):
    if not candles:
        raise UserInputError("нет свечей для backtest")

    previous_begin = None
    for candle in candles:
        if previous_begin is not None and candle.begin <= previous_begin:
            raise UserInputError("свечи должны идти по времени без повторов")
        previous_begin = candle.begin
        if min(candle.open, candle.close, candle.high, candle.low) <= 0:
            raise UserInputError(f"в свече {candle.begin} есть неположительная цена")
        if candle.high < max(candle.open, candle.close) or candle.low > min(candle.open, candle.close):
            raise UserInputError(f"в свече {candle.begin} неверные high/low")


def sma(values: list[Decimal], window: int) -> list[Decimal | None]:
    if window <= 0:
        raise UserInputError("окно SMA должно быть положительным")
    result = []
    rolling = Decimal("0")
    for index, value in enumerate(values):
        rolling += value
        if index >= window:
            rolling -= values[index - window]
        result.append(None if index + 1 < window else rolling / Decimal(window))
    return result


def execution_price(open_price, side, slippage_bps, min_step):
    direction = Decimal("1") if side == BUY else Decimal("-1")
    raw_price = open_price * (Decimal("1") + direction * slippage_bps / Decimal("10000"))
    rounding = ROUND_CEILING if side == BUY else ROUND_FLOOR
    steps = (raw_price / min_step).to_integral_value(rounding=rounding)
    return steps * min_step


def liquidity_limit(candle, instrument, max_volume_share_pct):
    if max_volume_share_pct == 0:
        return None
    if candle.volume is None:
        raise UserInputError("для ограничения ликвидности в свечах нужен volume")
    available_units = candle.volume * max_volume_share_pct / Decimal("100")
    return int((available_units / Decimal(instrument.lot_size)).to_integral_value(rounding=ROUND_FLOOR))


def closed_trade_pnls(fills):
    entry_cost = Decimal("0")
    exit_proceeds = Decimal("0")
    result = []
    for fill in fills:
        if fill.side == BUY:
            entry_cost += fill.value + fill.commission
        else:
            exit_proceeds += fill.value - fill.commission
            if fill.position_after == 0 and entry_cost:
                result.append(exit_proceeds - entry_cost)
                entry_cost = Decimal("0")
                exit_proceeds = Decimal("0")
    return result


def equity_metrics(equity_curve, initial_cash, fills):
    peak = equity_curve[0].equity
    max_drawdown = Decimal("0")
    current_drawdown_days = 0
    max_drawdown_days = 0
    exposed_days = 0
    returns = []

    for index, point in enumerate(equity_curve):
        if point.position_lots > 0:
            exposed_days += 1
        if point.equity >= peak:
            peak = point.equity
            current_drawdown_days = 0
        else:
            current_drawdown_days += 1
            max_drawdown_days = max(max_drawdown_days, current_drawdown_days)
        drawdown = point.equity / peak - Decimal("1") if peak else Decimal("0")
        max_drawdown = min(max_drawdown, drawdown)
        if index > 0 and equity_curve[index - 1].equity:
            returns.append(float(point.equity / equity_curve[index - 1].equity - Decimal("1")))

    annual_volatility = None
    sharpe = None
    if len(returns) > 1:
        average = sum(returns) / len(returns)
        variance = sum((value - average) ** 2 for value in returns) / (len(returns) - 1)
        daily_volatility = math.sqrt(variance)
        annual_volatility = daily_volatility * math.sqrt(252) * 100
        if daily_volatility:
            sharpe = average / daily_volatility * math.sqrt(252)

    trade_pnls = closed_trade_pnls(fills)
    wins = [pnl for pnl in trade_pnls if pnl > 0]
    losses = [pnl for pnl in trade_pnls if pnl < 0]
    total_commission = sum((fill.commission for fill in fills), Decimal("0"))
    final_equity = equity_curve[-1].equity

    try:
        first_day = datetime.fromisoformat(equity_curve[0].date).date()
        last_day = datetime.fromisoformat(equity_curve[-1].date).date()
        calendar_days = (last_day - first_day).days
    except ValueError:
        calendar_days = 0
    cagr = None
    if calendar_days > 0 and final_equity > 0:
        cagr = (float(final_equity / initial_cash) ** (365 / calendar_days) - 1) * 100

    return {
        "max_drawdown_pct": str((max_drawdown * Decimal("100")).quantize(Decimal("0.01"))),
        "max_drawdown_days": max_drawdown_days,
        "exposure_pct": str(
            (Decimal(exposed_days) / Decimal(len(equity_curve)) * Decimal("100")).quantize(Decimal("0.01"))
        ),
        "annual_volatility_pct": None if annual_volatility is None else f"{annual_volatility:.2f}",
        "sharpe_zero_rate": None if sharpe is None else f"{sharpe:.3f}",
        "cagr_pct": None if cagr is None else f"{cagr:.2f}",
        "closed_trades": len(trade_pnls),
        "win_rate_pct": None if not trade_pnls else f"{len(wins) / len(trade_pnls) * 100:.2f}",
        "average_win_rub": None if not wins else str((sum(wins) / len(wins)).quantize(Decimal("0.01"))),
        "average_loss_rub": None if not losses else str((sum(losses) / len(losses)).quantize(Decimal("0.01"))),
        "total_commission_rub": str(total_commission.quantize(Decimal("0.01"))),
    }


def run_sma_backtest(
    candles: list[Candle],
    instrument: Instrument,
    fast: int,
    slow: int,
    initial_cash: Decimal,
    commission_bps: Decimal,
    slippage_bps: Decimal = Decimal("0"),
    max_volume_share_pct: Decimal = Decimal("0"),
) -> BacktestResult:
    if fast >= slow:
        raise UserInputError("fast SMA должна быть меньше slow SMA")
    if initial_cash <= 0:
        raise UserInputError("начальный капитал должен быть положительным")
    if commission_bps < 0 or slippage_bps < 0:
        raise UserInputError("комиссия и проскальзывание не могут быть отрицательными")
    if slippage_bps >= Decimal("10000"):
        raise UserInputError("проскальзывание должно быть меньше 10000 bps")
    if not Decimal("0") <= max_volume_share_pct <= Decimal("100"):
        raise UserInputError("доля дневного объёма должна быть от 0 до 100 процентов")
    if len(candles) < slow + 2:
        raise UserInputError("недостаточно свечей для выбранных SMA")
    validate_candles(candles)

    closes = [candle.close for candle in candles]
    fast_sma = sma(closes, fast)
    slow_sma = sma(closes, slow)
    commission_rate = commission_bps / Decimal("10000")

    cash = initial_cash
    position_lots = 0
    fills = []
    equity_curve = []

    for signal_index in range(len(candles) - 1):
        today = candles[signal_index]
        tomorrow = candles[signal_index + 1]
        today_equity = cash + today.close * Decimal(instrument.lot_size) * Decimal(position_lots)
        equity_curve.append(
            EquityPoint(
                date=today.begin,
                close=today.close,
                cash=cash,
                position_lots=position_lots,
                equity=today_equity,
            )
        )

        target_long = (
            fast_sma[signal_index] is not None
            and slow_sma[signal_index] is not None
            and fast_sma[signal_index] > slow_sma[signal_index]
        )
        max_lots = liquidity_limit(tomorrow, instrument, max_volume_share_pct)

        if target_long and position_lots == 0:
            price = execution_price(tomorrow.open, BUY, slippage_bps, instrument.min_step)
            one_lot_value = price * Decimal(instrument.lot_size)
            affordable = int(
                (cash / (one_lot_value * (Decimal("1") + commission_rate))).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            )
            lots = affordable if max_lots is None else min(affordable, max_lots)
            if lots > 0:
                value = one_lot_value * Decimal(lots)
                commission = value * commission_rate
                cash -= value + commission
                position_lots = lots
                fills.append(
                    Fill(
                        date=tomorrow.begin,
                        side=BUY,
                        price=price,
                        lots=lots,
                        value=value,
                        commission=commission,
                        cash_after=cash,
                        position_after=position_lots,
                    )
                )
        elif not target_long and position_lots > 0:
            price = execution_price(tomorrow.open, SELL, slippage_bps, instrument.min_step)
            lots = position_lots if max_lots is None else min(position_lots, max_lots)
            if lots > 0:
                value = price * Decimal(instrument.lot_size) * Decimal(lots)
                commission = value * commission_rate
                cash += value - commission
                position_lots -= lots
                fills.append(
                    Fill(
                        date=tomorrow.begin,
                        side=SELL,
                        price=price,
                        lots=lots,
                        value=value,
                        commission=commission,
                        cash_after=cash,
                        position_after=position_lots,
                    )
                )

    last = candles[-1]
    final_equity = cash + last.close * Decimal(instrument.lot_size) * Decimal(position_lots)
    equity_curve.append(
        EquityPoint(
            date=last.begin,
            close=last.close,
            cash=cash,
            position_lots=position_lots,
            equity=final_equity,
        )
    )

    first_signal_index = next(
        index
        for index in range(len(candles) - 1)
        if fast_sma[index] is not None and slow_sma[index] is not None
    )
    benchmark_index = first_signal_index + 1
    benchmark_candle = candles[benchmark_index]
    benchmark_price = execution_price(benchmark_candle.open, BUY, slippage_bps, instrument.min_step)
    benchmark_lot_value = benchmark_price * Decimal(instrument.lot_size)
    buy_hold_lots = int(
        (initial_cash / (benchmark_lot_value * (Decimal("1") + commission_rate))).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    buy_hold_cash = initial_cash
    if buy_hold_lots > 0:
        buy_value = benchmark_lot_value * Decimal(buy_hold_lots)
        buy_hold_cash -= buy_value + buy_value * commission_rate
    buy_hold_equity = buy_hold_cash + last.close * Decimal(instrument.lot_size) * Decimal(buy_hold_lots)

    total_return = final_equity / initial_cash - Decimal("1")
    buy_hold_return = buy_hold_equity / initial_cash - Decimal("1")
    summary = {
        "instrument": instrument.security_id,
        "strategy": "SMA long/cash",
        "strategy_version": 2,
        "timing": "close[t] -> signal -> open[t+1]",
        "candles": len(candles),
        "from": candles[0].begin,
        "till": candles[-1].begin,
        "fast_sma": fast,
        "slow_sma": slow,
        "initial_cash": str(initial_cash),
        "final_equity": str(final_equity.quantize(Decimal("0.01"))),
        "total_return_pct": str((total_return * Decimal("100")).quantize(Decimal("0.01"))),
        "buy_hold_start": benchmark_candle.begin,
        "buy_hold_equity": str(buy_hold_equity.quantize(Decimal("0.01"))),
        "buy_hold_return_pct": str((buy_hold_return * Decimal("100")).quantize(Decimal("0.01"))),
        "fills": len(fills),
        "trades": len(fills),
        "final_position_lots": position_lots,
        "commission_bps": str(commission_bps),
        "slippage_bps": str(slippage_bps),
        "max_volume_share_pct": str(max_volume_share_pct),
    }
    summary.update(equity_metrics(equity_curve, initial_cash, fills))
    return BacktestResult(summary=summary, fills=fills, equity_curve=equity_curve, candles=candles)


def write_csv(path, headers, rows):
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(headers)
        writer.writerows(rows)


def save_report(result: BacktestResult, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(result.summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_csv(
        output_dir / "equity_curve.csv",
        ["date", "close", "cash", "position_lots", "equity"],
        [
            [point.date, point.close, point.cash, point.position_lots, point.equity]
            for point in result.equity_curve
        ],
    )
    write_csv(
        output_dir / "fills.csv",
        ["date", "side", "price", "lots", "value", "commission", "cash_after", "position_after"],
        [
            [
                fill.date,
                fill.side,
                fill.price,
                fill.lots,
                fill.value,
                fill.commission,
                fill.cash_after,
                fill.position_after,
            ]
            for fill in result.fills
        ],
    )
    write_csv(
        output_dir / "candles.csv",
        ["begin", "end", "open", "close", "high", "low", "value", "volume"],
        [
            [
                candle.begin,
                candle.end,
                candle.open,
                candle.close,
                candle.high,
                candle.low,
                candle.value,
                candle.volume,
            ]
            for candle in result.candles
        ],
    )


def print_summary(summary, output_dir):
    print(f"Backtest {summary['instrument']}: {summary['strategy']}")
    print(f"Period: {summary['from']} -> {summary['till']} ({summary['candles']} candles)")
    print(f"SMA: fast={summary['fast_sma']}, slow={summary['slow_sma']}; timing: {summary['timing']}")
    print(
        "Result: "
        f"{summary['initial_cash']} RUB -> {summary['final_equity']} RUB "
        f"({summary['total_return_pct']}%)"
    )
    print(
        "Buy&hold: "
        f"{summary['buy_hold_equity']} RUB ({summary['buy_hold_return_pct']}%) "
        f"from {summary['buy_hold_start']}"
    )
    print(
        f"Risk: max drawdown {summary['max_drawdown_pct']}%; "
        f"Sharpe {summary['sharpe_zero_rate']}; exposure {summary['exposure_pct']}%"
    )
    print(
        f"Trades: fills {summary['fills']}; closed {summary['closed_trades']}; "
        f"win rate {summary['win_rate_pct']}%; commission {summary['total_commission_rub']} RUB"
    )
    print(f"Report: {output_dir}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MOEX candle SMA backtest for SBER or CNYRUB_TOM.")
    parser.add_argument("ticker", nargs="?", default="SBER")
    parser.add_argument("market", nargs="?", default="TQBR")
    parser.add_argument("--from", dest="date_from", default="2025-01-01")
    parser.add_argument("--till", dest="date_till", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--fast", type=int, default=10)
    parser.add_argument("--slow", type=int, default=30)
    parser.add_argument("--cash", type=Decimal, default=Decimal("1000000"))
    parser.add_argument("--commission-bps", type=Decimal, default=Decimal("2"))
    parser.add_argument("--slippage-bps", type=Decimal, default=Decimal("0"))
    parser.add_argument("--max-volume-share", type=Decimal, default=Decimal("0"))
    parser.add_argument("--interval", type=int, default=24)
    parser.add_argument("--candles-file", type=Path)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--out-dir", type=Path, default=Path("reports"))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command_args = list(argv) if argv is not None else sys.argv[1:]
    try:
        instrument = load_instrument(args.ticker, args.market, args.data_dir)
        if args.candles_file:
            candles = load_candles_csv(args.candles_file)
            data_source = str(args.candles_file.resolve())
        else:
            validate_dates(args.date_from, args.date_till)
            candles = download_candles(instrument, args.date_from, args.date_till, args.interval)
            data_source = "MOEX ISS"
        validate_candles(candles)

        result = run_sma_backtest(
            candles=candles,
            instrument=instrument,
            fast=args.fast,
            slow=args.slow,
            initial_cash=args.cash,
            commission_bps=args.commission_bps,
            slippage_bps=args.slippage_bps,
            max_volume_share_pct=args.max_volume_share,
        )
        result.summary.update(
            {
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "data_source": data_source,
                "command": [sys.executable, str(Path(__file__).resolve()), *command_args],
            }
        )
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = args.out_dir / f"{instrument.ticker}_{instrument.market}_{stamp}"
        save_report(result, output_dir)
        print_summary(result.summary, output_dir)
        return 0
    except (UserInputError, RuntimeError, OSError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
