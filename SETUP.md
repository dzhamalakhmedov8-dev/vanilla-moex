# Setup

Проект рассчитан на Python 3.11+.

## Установка

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Проверить версию Python:

```powershell
.\.venv\Scripts\python.exe --version
```

## Dry-run

Dry-run не подключается к RabbitMQ и не отправляет заявку:

```powershell
.\.venv\Scripts\python.exe vanilla_bot.py SBER TQBR --qty 1 --price 300
.\.venv\Scripts\python.exe vanilla_bot.py CNYRUB_TOM CETS --volume 2000000 --slippage 0.1 --last-price 10.46
```

## Live

Для live-запуска нужны реквизиты RabbitMQ из задания. Не храните реальные доступы в git.

```powershell
$env:RABBITMQ_URL="<rabbitmq-url-from-task>"
$env:VANILLA_OWNER="dakhmedov"
.\.venv\Scripts\python.exe vanilla_bot.py SBER TQBR --qty 1 --best-quote --live
```

`VANILLA_OWNER` попадает в имена временных очередей и в `OrderStrategyId`.

## Проверки

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -v
.\.venv\Scripts\python.exe -B -m py_compile trading_core.py vanilla_bot.py backtest_moex.py scripts\rabbitmq_smoke.py tests\test_vanilla_bot.py tests\test_backtest_moex.py
.\.venv\Scripts\python.exe -m pip check
```

## CI

GitHub Actions проверяет:

- unit-тесты;
- CLI smoke для `vanilla_bot.py` и `backtest_moex.py`;
- ошибки CLI без `qty/volume` и без способа цены;
- локальный RabbitMQ smoke test через service container;
- `py_compile`;
- `pip check`.

Если локально запущен RabbitMQ, можно проверить базовую работу `aio-pika` без учебного сервера:

```powershell
$env:RABBITMQ_URL="amqp://guest:guest@localhost:5672/"
.\.venv\Scripts\python.exe scripts\rabbitmq_smoke.py
```

## Что не публикуется

В git не попадают `.env`, `vanilla.md`, отчеты `reports/`, конспекты, PDF, `.venv` и локальные служебные папки.
