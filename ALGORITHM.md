# Алгоритм Vanilla

1. Прочитать из командной строки `ticker`, `market`, `qty` или `volume`, а также `price`, `slippage` или `best_quote`.
2. Проверить, что инструмент один из двух разрешённых: `SBER/TQBR` или `CNYRUB_TOM/CETS`.
3. Загрузить параметры инструмента из JSON-файла рядом со скриптом.
4. Определить сторону заявки: положительное значение означает покупку, отрицательное - продажу.
5. Подключиться к RabbitMQ.
6. Создать очереди для статусов, сделок, тиков и стаканов.
7. Если цена задана через `slippage` или `best_quote`, дождаться нужных рыночных данных.
8. Посчитать лимитную цену и количество лотов.
9. Отправить одну лимитную заявку в `sandbox.orders`.
10. Дождаться статуса со своим `OrderStrategyId` и получить номер заявки.
11. Дождаться финального статуса `Filled` или `Cancelled`.
12. Собрать сделки по полям `orderid`, `tradeid`, `securityname`.
13. Напечатать итог: исполненные лоты, сумма, средняя цена и количество сделок.
14. Удалить временные очереди и закрыть соединение.

## Примеры

```powershell
$env:RABBITMQ_URL="<rabbitmq-url-from-task>"
$env:VANILLA_OWNER="dakhmedov"

.\.venv\Scripts\python.exe vanilla_bot.py SBER TQBR --qty 1 --price 300
.\.venv\Scripts\python.exe vanilla_bot.py SBER TQBR --volume 10000 --slippage 0.2
.\.venv\Scripts\python.exe vanilla_bot.py CNYRUB_TOM CETS --qty -1 --best-quote
```
