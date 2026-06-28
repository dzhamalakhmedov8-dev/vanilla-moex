# Backtest

`backtest_moex.py` проверяет стратегию `SMA long/cash` на истории и никогда не отправляет заявки.

## Модель

- сигнал: закрытие дня `t`;
- исполнение: открытие следующего дня `t+1`;
- позиция: только long или деньги;
- комиссия: `--commission-bps`, по умолчанию 2 bps;
- проскальзывание: `--slippage-bps`, по умолчанию 0;
- лимит ликвидности: `--max-volume-share`, процент дневного объёма, 0 отключает лимит;
- benchmark buy-and-hold начинает торговлю после прогрева slow SMA.

## Загрузка с MOEX

```powershell
.\.venv\Scripts\python.exe backtest_moex.py SBER TQBR --from 2025-01-01 --till 2026-06-19 --fast 10 --slow 30 --cash 1000000
```

Реалистичнее исполнение:

```powershell
.\.venv\Scripts\python.exe backtest_moex.py SBER TQBR --from 2025-01-01 --till 2026-06-19 --slippage-bps 5 --max-volume-share 1
```

## Повторный запуск без сети

Каждый отчёт сохраняет `candles.csv`. Его можно использовать повторно:

```powershell
.\.venv\Scripts\python.exe backtest_moex.py SBER TQBR --candles-file reports\SBER_TQBR_YYYYMMDD_HHMMSS\candles.csv
```

## Отчёт

- `summary.json`: параметры запуска, источник данных, доходность и риск;
- `equity_curve.csv`: капитал по дням;
- `fills.csv`: исполнения;
- `candles.csv`: точный набор исходных данных.

В `summary.json` записываются доходность, CAGR, максимальная и длительная просадка, волатильность, Sharpe с нулевой безрисковой ставкой, время в позиции, win rate и комиссии.

Backtest всё ещё является упрощением. Он не моделирует очередь заявок, внутридневное изменение спреда, задержки сети и влияние крупных заявок на рынок.
