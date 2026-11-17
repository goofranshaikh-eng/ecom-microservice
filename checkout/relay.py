import json
import os
import time

import pika

from db import outbox_collection
from outbox import mark_published
from rabbitmq_config import EXCHANGE_NAME, get_rabbitmq_connection

# [CONFIG_DEFERRED] poll interval is read from the environment; default 1 second.
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "1"))

ROUTING_KEYS = {
    "OrderRequested": "order.requested",
    "RefundRequested": "payment.refund.requested",
}


def publish_outbox_messages() -> None:
    """
    Poll outbox_collection for unpublished rows, publish to RabbitMQ, mark published.
    Runs forever (polls every POLL_INTERVAL_SECONDS, default 1 second).
    """
    connection = get_rabbitmq_connection()
    channel = connection.channel()
    channel.exchange_declare(
        exchange=EXCHANGE_NAME, exchange_type="topic", durable=True
    )
    # basic_publish raises unless the broker acks when confirms are enabled.
    channel.confirm_delivery()

    while True:
        rows = outbox_collection.find({"published": False}).limit(100)

        for row in rows:
            event_type = row["event_type"]
            payload = row["payload"]
            row_id = row["_id"]

            routing_key = ROUTING_KEYS.get(event_type)

            if routing_key is None:
                print(f"Unknown event_type: {event_type}, skipping row {row_id}")
                continue

            try:
                channel.basic_publish(
                    exchange=EXCHANGE_NAME,
                    routing_key=routing_key,
                    body=json.dumps(payload),
                    properties=pika.BasicProperties(
                        delivery_mode=2, content_type="application/json"
                    ),
                )
                mark_published(row_id)
                print(
                    f"Published {event_type} for payment_reference {row['payment_reference']}"
                )
            except Exception as exc:
                # Leave the row unpublished so the next poll retries.
                # A crash after publish and before mark_published re-publishes.
                print(f"Failed to publish row {row_id}: {exc}")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    publish_outbox_messages()
