# Алгоритм Vanilla

1. Прочитать из командной строки `ticker`, `market`, `qty` или `volume`, а также способ задания цены.
2. Загрузить параметры инструмента из `SBER.json` или `CNYRUB_TOM.json`.
3. Определить сторону заявки: положительное значение означает покупку, отрицательное — продажу.
4. Выбрать способ цены с приоритетом `price > slippage > best_quote`.
5. Подключиться к RabbitMQ.
6. Создать временные очереди:
   - статусы из `sandbox.order.status`;
   - сделки из `sandbox.trades`;
   - тики из `marketdata.ticks.alor`;
   - стаканы из `marketdata.orderbooks.alor`.
7. Для `slippage` дождаться последней цены, для `best_quote` дождаться нужной стороны стакана.
8. Посчитать лимитную цену и количество лотов.
9. Отправить одну лимитную заявку в `sandbox.orders` с routing key `locko.place`.
10. Дождаться статуса со своим `OrderStrategyId` и получить `orderId`.
11. Дождаться финального статуса `Filled` или `Cancelled`.
12. Собрать сделки по `orderId`, включая сделки, которые могли прийти раньше статуса.
13. Напечатать итог: исполненные лоты, сумма в рублях, средняя цена и количество сделок.
14. Удалить временные очереди и закрыть соединение.

## Примеры

```powershell
$env:RABBITMQ_URL="<rabbitmq-url-from-task>"
$env:VANILLA_OWNER="dakhmedov"

.\.venv\Scripts\python.exe vanilla_bot.py SBER TQBR --qty 1 --price 300
.\.venv\Scripts\python.exe vanilla_bot.py SBER TQBR --volume 10000 --slippage 0.2
.\.venv\Scripts\python.exe vanilla_bot.py CNYRUB_TOM CETS --qty -1 --best-quote
```
