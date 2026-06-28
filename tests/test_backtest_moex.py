import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest_moex import (  # noqa: E402
    Candle,
    load_candles_csv,
    parse_candles,
    run_sma_backtest,
    save_report,
    sma,
)
from trading_core import load_instrument  # noqa: E402


def candle(day: int, open_price: str, close_price: str, volume: str | None = None) -> Candle:
    date = f"2026-01-{day:02d} 00:00:00"
    open_value = Decimal(open_price)
    close_value = Decimal(close_price)
    return Candle(
        begin=date,
        end=f"2026-01-{day:02d} 23:59:59",
        open=open_value,
        close=close_value,
        high=max(open_value, close_value),
        low=min(open_value, close_value),
        value=None,
        volume=Decimal(volume) if volume is not None else None,
    )


class BacktestMoexTests(unittest.TestCase):
    def test_sma_returns_none_until_window_is_ready(self) -> None:
        values = [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")]
        self.assertEqual(sma(values, 3), [None, None, Decimal("2"), Decimal("3")])

    def test_parse_candles(self) -> None:
        raw = {
            "candles": {
                "columns": ["open", "close", "high", "low", "value", "volume", "begin", "end"],
                "data": [[300, 301, 302, 299, 1000, 10, "2026-01-01", "2026-01-01"]],
            }
        }
        parsed = parse_candles(raw)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].open, Decimal("300"))
        self.assertEqual(parsed[0].close, Decimal("301"))

    def test_signal_executes_on_next_open_without_lookahead(self) -> None:
        instrument = load_instrument("SBER", "TQBR", ROOT)
        candles = [
            candle(1, "100", "100"),
            candle(2, "100", "100"),
            candle(3, "100", "100"),
            candle(4, "100", "120"),
            candle(5, "111", "130"),
            candle(6, "125", "90"),
            candle(7, "80", "80"),
            candle(8, "75", "75"),
        ]
        result = run_sma_backtest(
            candles=candles,
            instrument=instrument,
            fast=2,
            slow=3,
            initial_cash=Decimal("1000"),
            commission_bps=Decimal("0"),
        )
        self.assertGreaterEqual(len(result.fills), 1)
        first_fill = result.fills[0]
        self.assertEqual(first_fill.side, "BUY")
        self.assertEqual(first_fill.date, "2026-01-05 00:00:00")
        self.assertEqual(first_fill.price, Decimal("111"))
        self.assertEqual(result.summary["buy_hold_start"], "2026-01-04 00:00:00")

    def test_slippage_and_liquidity_limit_change_fill(self) -> None:
        instrument = load_instrument("SBER", "TQBR", ROOT)
        candles = [
            candle(1, "100", "100", "100"),
            candle(2, "100", "100", "100"),
            candle(3, "100", "100", "100"),
            candle(4, "100", "120", "100"),
            candle(5, "111", "130", "5"),
            candle(6, "125", "90", "100"),
            candle(7, "80", "80", "100"),
            candle(8, "75", "75", "100"),
        ]
        result = run_sma_backtest(
            candles=candles,
            instrument=instrument,
            fast=2,
            slow=3,
            initial_cash=Decimal("1000"),
            commission_bps=Decimal("0"),
            slippage_bps=Decimal("100"),
            max_volume_share_pct=Decimal("20"),
        )
        self.assertEqual(result.fills[0].price, Decimal("112.11"))
        self.assertEqual(result.fills[0].lots, 1)

    def test_saved_candles_can_be_loaded_again(self) -> None:
        instrument = load_instrument("SBER", "TQBR", ROOT)
        candles = [candle(day, "100", str(100 + day)) for day in range(1, 8)]
        result = run_sma_backtest(
            candles=candles,
            instrument=instrument,
            fast=2,
            slow=3,
            initial_cash=Decimal("1000"),
            commission_bps=Decimal("0"),
        )
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            save_report(result, output_dir)
            loaded = load_candles_csv(output_dir / "candles.csv")
        self.assertEqual(loaded, candles)


if __name__ == "__main__":
    unittest.main()
