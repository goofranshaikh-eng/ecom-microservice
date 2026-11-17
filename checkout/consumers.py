import asyncio
import json
from datetime import datetime

import httpx

from db import checkouts_collection
from rabbitmq_config import get_rabbitmq_connection, setup_exchange_and_queue

# [SERVICE_URLS_HARDCODED] same cart base URL as routes.py. [CONFIG_DEFERRED]
CART_SERVICE_URL = "http://localhost:8003"
ORDER_CREATED_QUEUE = "order.created.queue"
ORDER_CREATED_ROUTING_KEY = "order.created"

# Checkout reads and writes only checkout_db.checkouts. Cart removal is HTTP.


def is_valid_order_created(msg: dict) -> bool:
    return (
        msg.get("event_type") == "OrderCreated"
        and isinstance(msg.get("payment_reference"), str)
        and len(msg.get("payment_reference", "")) > 0
        and isinstance(msg.get("order_id"), str)
        and len(msg.get("order_id", "")) > 0
    )


async def delete_cart_with_retry(
    cart_id: str,
    max_retries: int = 3,
    delays: list[int] | None = None,
) -> bool:
    """
    Attempt to delete a cart with exponential backoff retry.

    Returns True when the cart is gone (204 or 404). Returns False when every
    attempt failed. Network errors and non-success statuses are retried.
    No exceptions are raised.
    """
    if delays is None:
        delays = [5, 10, 20]

    cart_url = f"{CART_SERVICE_URL}/cart/{cart_id}"

    for retry_count in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.delete(cart_url)

            if response.status_code == 204:
                print(f"Cart {cart_id} deleted successfully")
                return True

            if response.status_code == 404:
                print(f"Cart {cart_id} not found (already deleted or never existed)")
                return True

            print(
                f"Cart delete failed (attempt {retry_count + 1}/{max_retries + 1}): "
                f"status {response.status_code}"
            )
        except httpx.RequestError as exc:
            print(
                f"Cart delete network error (attempt {retry_count + 1}/{max_retries + 1}): "
                f"{exc}"
            )

        if retry_count < max_retries:
            await asyncio.sleep(delays[retry_count])

    print(f"Cart delete exhausted all retries for cart {cart_id}")
    return False


def on_message(channel, method, properties, body):
    try:
        msg = json.loads(body.decode())
    except json.JSONDecodeError:
        print(f"Failed to parse OrderCreated message: {body}")
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    if not is_valid_order_created(msg):
        print(f"Invalid OrderCreated message: {msg}")
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    payment_reference = msg["payment_reference"]
    order_id = msg["order_id"]
    print(
        f"Received OrderCreated payment_reference {payment_reference} "
        f"order_id {order_id} timestamp {msg.get('timestamp')}"
    )

    checkout = checkouts_collection.find_one({"payment_reference": payment_reference})
    if checkout is None:
        print(
            f"Checkout not found for payment_reference {payment_reference}, "
            "orphaned OrderCreated"
        )
        channel.basic_ack(delivery_tag=method.delivery_tag)
        return

    status = checkout["status"]
    if status in ["order_created", "cart_cleared"]:
        print(
            f"OrderCreated already processed for payment_reference {payment_reference}, "
            f"status={status}"
        )
        channel.basic_ack(delivery_tag=method.delivery_tag)
        return

    if status == "refunded":
        print(
            f"OrderCreated received for refunded checkout, "
            f"payment_reference {payment_reference}"
        )
        channel.basic_ack(delivery_tag=method.delivery_tag)
        return

    checkouts_collection.update_one(
        {"payment_reference": payment_reference},
        {
            "$set": {
                "status": "order_created",
                "order_id": order_id,
                "updated_at": datetime.utcnow(),
            }
        },
    )
    print(f"Updated checkout to order_created for payment_reference {payment_reference}")

    cart_id = checkout["cart_id"]
    # pika invokes this callback on its blocking thread, which has no running loop.
    loop = asyncio.new_event_loop()
    try:
        success = loop.run_until_complete(delete_cart_with_retry(cart_id))
    finally:
        loop.close()

    if success:
        checkouts_collection.update_one(
            {"payment_reference": payment_reference},
            {"$set": {"status": "cart_cleared", "updated_at": datetime.utcnow()}},
        )
        print(
            f"Updated checkout to cart_cleared for payment_reference {payment_reference}"
        )
    else:
        print(
            f"[KNOWN_GAP] Cart delete failed for cart {cart_id}, "
            "checkout status remains order_created"
        )

    channel.basic_ack(delivery_tag=method.delivery_tag)


def _consume_blocking() -> None:
    connection = get_rabbitmq_connection()
    channel = connection.channel()
    setup_exchange_and_queue(
        channel, ORDER_CREATED_ROUTING_KEY, ORDER_CREATED_QUEUE
    )
    channel.basic_qos(prefetch_count=1)
    print("OrderCreated consumer started, waiting for messages...")
    channel.basic_consume(
        queue=ORDER_CREATED_QUEUE,
        on_message_callback=on_message,
        auto_ack=False,
    )
    channel.start_consuming()


async def consume_order_created() -> None:
    """
    Connect to RabbitMQ and consume order.created.queue until the process exits.

    The blocking client runs on a worker thread so the FastAPI event loop stays free.
    """
    try:
        await asyncio.to_thread(_consume_blocking)
    except Exception as exc:
        print(f"Failed to start OrderCreated consumer: {exc}")
        raise
