# Алгоритм Vanilla

1. Прочитать `ticker`, `market`, `qty` или `volume` и способ определения цены.
2. Загрузить и проверить параметры инструмента.
3. Определить сторону: положительное значение означает покупку, отрицательное продажу.
4. Выбрать способ цены с приоритетом `price > slippage > best_quote`.
5. В режиме по умолчанию рассчитать заявку, показать JSON и завершить работу без RabbitMQ.
6. При `--live` проверить наличие `RABBITMQ_URL` и подключиться к серверу.
7. Создать временные очереди статусов, сделок, тиков и стаканов.
8. Для `slippage` дождаться последней цены, для `best_quote` дождаться ask или bid.
9. Рассчитать цену с учётом `MINSTEP` и количество целых лотов.
10. Отправить одну лимитную заявку в `sandbox.orders` с routing key `locko.place`.
11. По `strategyName` получить `orderId`, затем дождаться `Filled` или `Cancelled`.
12. Собрать сделки по `orderId`, исключая повторы и учитывая сообщения, пришедшие раньше статуса.
13. Вывести исполненный объём, сумму, среднюю цену и число сделок.
14. Удалить временные очереди и закрыть соединение.

## Примеры

```powershell
# Dry-run по умолчанию
.\.venv\Scripts\python.exe vanilla_bot.py SBER TQBR --qty 1 --price 300

# Dry-run с расчётом от тестовой последней цены
.\.venv\Scripts\python.exe vanilla_bot.py SBER TQBR --volume 10000 --slippage 0.2 --last-price 322.30

# Явный live-запуск
$env:RABBITMQ_URL="amqp://user:password@host:5672/"
.\.venv\Scripts\python.exe vanilla_bot.py SBER TQBR --qty 1 --best-quote --live
```
