# Vanilla MOEX

Учебный Python-проект для демонстрации двух вещей:

- отправка одной лимитной заявки в RabbitMQ-песочницу MOEX;
- простой backtest SMA-стратегии на дневных свечах MOEX.

По умолчанию проект работает безопасно: `vanilla_bot.py` запускается в `dry-run` и не отправляет заявку без явного флага `--live`.

## Что внутри

- `trading_core.py` - общие расчеты заявки и проверка инструментов.
- `vanilla_bot.py` - CLI для dry-run и live-запуска через RabbitMQ.
- `backtest_moex.py` - загрузка свечей MOEX, SMA-сигналы, комиссии, проскальзывание и отчет.
- `tests/` - unit-тесты для расчета заявки, live-flow через fake RabbitMQ и backtest.
- `SBER.json`, `CNYRUB_TOM.json` - описания инструментов.
- `ARCHITECTURE.md`, `ALGORITHM.md`, `BACKTEST.md`, `PROJECT_PLAN.md` - короткая документация.

## Установка

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Быстрая проверка заявки

Без `--live` заявка не отправляется:

```powershell
.\.venv\Scripts\python.exe vanilla_bot.py SBER TQBR --qty 1 --price 300
```

Пример dry-run с расчетом цены от последней сделки:

```powershell
.\.venv\Scripts\python.exe vanilla_bot.py SBER TQBR --volume 10000 --slippage 0.2 --last-price 322.30
```

## Backtest

```powershell
.\.venv\Scripts\python.exe backtest_moex.py SBER TQBR --from 2025-01-01 --till 2026-06-19 --fast 10 --slow 30 --cash 1000000
```

Более консервативный запуск с проскальзыванием и лимитом ликвидности:

```powershell
.\.venv\Scripts\python.exe backtest_moex.py SBER TQBR --from 2025-01-01 --till 2026-06-19 --slippage-bps 5 --max-volume-share 1
```

## Live-песочница

Реквизиты RabbitMQ не хранятся в коде. Для live-запуска нужно задать переменную окружения и явно указать `--live`:

```powershell
$env:RABBITMQ_URL="amqp://user:password@host:5672/"
.\.venv\Scripts\python.exe vanilla_bot.py SBER TQBR --qty 1 --best-quote --live
```

## Проверки

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -v
.\.venv\Scripts\python.exe -B -m py_compile trading_core.py vanilla_bot.py backtest_moex.py
.\.venv\Scripts\python.exe -m pip check
```

## Что не попало в GitHub

В репозиторий не добавляются локальные отчеты, виртуальное окружение, конспекты, PDF и исходный файл задания с учебными учетными данными. Это нужно, чтобы ссылку можно было спокойно отправить другому человеку.
