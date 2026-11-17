import asyncio
import json
from datetime import datetime

from pymongo.errors import PyMongoError

from db import charges_collection
from rabbitmq_config import EXCHANGE_NAME, get_rabbitmq_connection

# Payment reads and writes only payment_db.charges.
REFUND_QUEUE = "payment.refund.requested.queue"
REFUND_ROUTING_KEY = "payment.refund.requested"


def is_valid_refund_requested(msg: dict) -> bool:
    """
    Validate RefundRequested message schema.
    Returns True if valid, False otherwise.
    """
    return (
        msg.get("event_type") == "RefundRequested"
        and isinstance(msg.get("payment_reference"), str)
        and len(msg.get("payment_reference", "")) > 0
    )


def on_message(channel, method, properties, body):
    try:
        msg = json.loads(body.decode())
    except json.JSONDecodeError:
        print(f"Failed to parse RefundRequested message: {body}")
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    if not is_valid_refund_requested(msg):
        print(f"Invalid RefundRequested message: {msg}")
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    payment_reference = msg["payment_reference"]
    # checkout_id and timestamp are for traceability only; they are not stored.
    print(
        f"Received RefundRequested payment_reference {payment_reference} "
        f"checkout_id {msg.get('checkout_id')} timestamp {msg.get('timestamp')}"
    )

    charge = charges_collection.find_one({"reference": payment_reference})
    if charge is None:
        print(
            f"Charge not found for payment_reference {payment_reference}, "
            "orphaned RefundRequested message"
        )
        channel.basic_ack(delivery_tag=method.delivery_tag)
        return

    status = charge["status"]
    if status == "refunded":
        print(f"Charge already refunded for payment_reference {payment_reference}")
        channel.basic_ack(delivery_tag=method.delivery_tag)
        return

    try:
        charges_collection.update_one(
            {"reference": payment_reference},
            {
                "$set": {
                    "status": "refunded",
                    "refunded_at": datetime.utcnow(),
                }
            },
        )
        print(f"Refunded charge for payment_reference {payment_reference}")
    except PyMongoError as exc:
        print(
            f"Failed to refund charge for payment_reference {payment_reference}: {exc}"
        )
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        return

    channel.basic_ack(delivery_tag=method.delivery_tag)


def _consume_blocking() -> None:
    connection = get_rabbitmq_connection()
    channel = connection.channel()

    channel.exchange_declare(
        exchange=EXCHANGE_NAME, exchange_type="topic", durable=True
    )
    channel.queue_declare(queue=REFUND_QUEUE, durable=True)
    channel.queue_bind(
        exchange=EXCHANGE_NAME, queue=REFUND_QUEUE, routing_key=REFUND_ROUTING_KEY
    )
    channel.basic_qos(prefetch_count=1)

    print("RefundRequested consumer started, waiting for messages...")
    channel.basic_consume(
        queue=REFUND_QUEUE, on_message_callback=on_message, auto_ack=False
    )
    channel.start_consuming()


async def consume_refund_requested() -> None:
    """
    Connect to RabbitMQ, subscribe to payment.refund.requested.queue, consume messages.

    For each message:
      1. Validate message body (JSON schema check: event_type, payment_reference)
      2. Query charges_collection by reference (msg["payment_reference"])
      3. If charge not found: log warning, ack message (orphaned message)
      4. If status is already "refunded": ack message (idempotency)
      5. Update status to "refunded", set refunded_at to now()
      6. Ack message after processing

    Runs forever until the process exits. The blocking client runs on a worker
    thread so the FastAPI event loop stays free.
    """
    try:
        await asyncio.to_thread(_consume_blocking)
    except Exception as exc:
        print(f"Failed to start RefundRequested consumer: {exc}")
        raise
