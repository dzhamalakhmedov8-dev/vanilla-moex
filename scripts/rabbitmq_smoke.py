import asyncio
import os
import uuid


async def run():
    import aio_pika

    url = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
    suffix = uuid.uuid4().hex[:8]
    connection = await aio_pika.connect_robust(url)
    try:
        channel = await connection.channel()
        exchange = await channel.declare_exchange(
            f"vanilla_smoke_{suffix}",
            aio_pika.ExchangeType.FANOUT,
            durable=False,
            auto_delete=True,
        )
        queue = await channel.declare_queue(
            f"vanilla_smoke_{suffix}",
            durable=False,
            exclusive=True,
            auto_delete=True,
            arguments={"x-max-length": 1000, "x-overflow": "drop-head"},
        )
        await queue.bind(exchange)

        loop = asyncio.get_running_loop()
        received = loop.create_future()

        async def on_message(message):
            async with message.process(requeue=False):
                if not received.done():
                    received.set_result(message.body)

        consumer_tag = await queue.consume(on_message)
        await exchange.publish(aio_pika.Message(body=b"ok"), routing_key="")
        body = await asyncio.wait_for(received, timeout=5)
        if body != b"ok":
            raise RuntimeError(f"unexpected smoke message: {body!r}")

        await queue.cancel(consumer_tag)
        await queue.delete(if_unused=False, if_empty=False)
        await exchange.delete(if_unused=False)
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(run())
    print("RabbitMQ smoke OK")
