import json
import os
import time
from pathlib import Path

import httpx
import pika
import pytest
from bson import ObjectId
from pymongo import MongoClient

# Service URLs (match checkout/routes.py constants)
CHECKOUT_URL = "http://localhost:8004"
CART_URL = "http://localhost:8003"
PAYMENT_URL = "http://localhost:8005"
ORDER_URL = "http://localhost:8006"

# MongoDB connection
MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")

# RabbitMQ connection
RABBITMQ_URI = "amqp://localhost:5672"


def setup_function():
    """Clear all databases before each test."""
    client = MongoClient(MONGODB_URI)
    
    for db_name in [
        "checkout_db",
        "order_db",
        "payment_db",
        "cart_db",
        "identity_db",
        "catalog_db",
    ]:
        db = client[db_name]
        for collection_name in db.list_collection_names():
            db[collection_name].delete_many({})
    
    client.close()
    
    # Verify environment variables for time-sensitive tests
    completion_window = os.environ.get("COMPLETION_WINDOW_SECONDS")
    if completion_window is None:
        pytest.skip("COMPLETION_WINDOW_SECONDS not set (required for AC6)")
    
    retry_delays = os.environ.get("CART_DELETE_RETRY_DELAYS")
    if retry_delays is None:
        pytest.skip("CART_DELETE_RETRY_DELAYS not set (required for AC5)")


def _create_cart(quantity: int = 2, price: int = 1500) -> str:
    """Seed a cart in cart_db and return cart_id."""
    client = MongoClient(MONGODB_URI)
    cart_db = client["cart_db"]
    
    cart_id = str(ObjectId())
    cart_db.carts.insert_one({
        "_id": cart_id,
        "lines": [
            {
                "product_id": "prod_001",
                "quantity": quantity,
                "price_snapshot_minor_units": price
            }
        ],
        "total_minor_units": quantity * price
    })
    
    client.close()
    return cart_id


def _ship_to() -> dict:
    """Return a valid ship_to address."""
    return {
        "street": "123 Main St",
        "city": "Springfield",
        "postal_code": "12345",
        "country": "US"
    }


def _poll_until(
    condition_fn,
    timeout_seconds: int = 10,
    poll_interval: float = 0.5
) -> bool:
    """
    Poll condition_fn() until it returns True or timeout is reached.
    
    Args:
        condition_fn: Callable that returns bool
        timeout_seconds: Maximum time to wait
        poll_interval: Time to wait between polls
    
    Returns:
        True if condition was met, False if timeout
    """
    start = time.time()
    while time.time() - start < timeout_seconds:
        if condition_fn():
            return True
        time.sleep(poll_interval)
    return False


@pytest.mark.asyncio
async def test_payment_refused_stays_immediate():
    """AC1: Payment refused stays immediate."""
    cart_id = _create_cart(quantity=1, price=1000)
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{CHECKOUT_URL}/checkout",
            json={
                "cart_id": cart_id,
                "email": "guest@example.com",
                "ship_to": _ship_to(),
            }
        )
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "refused"
    assert data["reason"] == "Card declined"
    assert "checkout_id" not in data
    
    # No checkout record
    mongo_client = MongoClient(MONGODB_URI)
    checkout_db = mongo_client["checkout_db"]
    assert checkout_db.checkouts.count_documents({}) == 0
    
    # No outbox row
    assert checkout_db.outbox.count_documents({}) == 0
    
    # Cart unchanged
    cart_db = mongo_client["cart_db"]
    cart = cart_db.carts.find_one({"_id": cart_id})
    assert cart is not None
    assert cart["total_minor_units"] == 1000
    
    # No order
    order_db = mongo_client["order_db"]
    assert order_db.orders.count_documents({}) == 0
    
    # No message published (verify queue is empty)
    connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URI))
    channel = connection.channel()
    method_frame, header_frame, body = channel.basic_get(queue="order.requested.queue", auto_ack=True)
    assert method_frame is None  # Queue is empty
    channel.close()
    connection.close()
    
    mongo_client.close()


@pytest.mark.asyncio
async def test_accepted_checkout_returns_before_order_exists():
    """AC2: Accepted checkout returns before the order exists."""
    cart_id = _create_cart(quantity=2, price=1500)
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{CHECKOUT_URL}/checkout",
            json={
                "cart_id": cart_id,
                "email": "guest@example.com",
                "ship_to": _ship_to()
            }
        )
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted"
    assert "checkout_id" in data
    assert "order_id" not in data  # Order not created yet
    
    checkout_id = data["checkout_id"]
    
    # Checkout record exists
    mongo_client = MongoClient(MONGODB_URI)
    checkout_db = mongo_client["checkout_db"]
    checkout = checkout_db.checkouts.find_one({"checkout_id": checkout_id})
    assert checkout is not None
    assert checkout["status"] == "accepted"
    assert "payment_reference" in checkout
    
    payment_reference = checkout["payment_reference"]
    
    # Outbox row exists
    outbox_row = checkout_db.outbox.find_one({"payment_reference": payment_reference})
    assert outbox_row is not None
    assert outbox_row["event_type"] == "OrderRequested"
    assert outbox_row["published"] == False
    
    # No order yet
    order_db = mongo_client["order_db"]
    assert order_db.orders.count_documents({"payment_reference": payment_reference}) == 0
    
    mongo_client.close()


@pytest.mark.manual
@pytest.mark.asyncio
async def test_order_created_after_recovery():
    """
    AC3: Order is created after the order service recovers.
    
    MANUAL TEST: Requires stopping and starting the order service.
    1. Stop the order service before running this test
    2. Run the test
    3. When prompted, start the order service
    """
    cart_id = _create_cart(quantity=2, price=1500)
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{CHECKOUT_URL}/checkout",
            json={
                "cart_id": cart_id,
                "email": "guest@example.com",
                "ship_to": _ship_to()
            }
        )
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted"
    checkout_id = data["checkout_id"]
    
    # Wait for relay to publish OrderRequested
    mongo_client = MongoClient(MONGODB_URI)
    checkout_db = mongo_client["checkout_db"]
    checkout = checkout_db.checkouts.find_one({"checkout_id": checkout_id})
    payment_reference = checkout["payment_reference"]
    
    published = _poll_until(
        lambda: checkout_db.outbox.find_one({"payment_reference": payment_reference, "published": True}) is not None,
        timeout_seconds=10
    )
    assert published, "Relay did not publish OrderRequested within 10 seconds"
    
    # Assert no order exists yet (order service is down)
    order_db = mongo_client["order_db"]
    assert order_db.orders.count_documents({"payment_reference": payment_reference}) == 0
    
    # Manual step: start order service
    print("Start order service manually, then press Enter")
    input()
    
    # Poll for order creation
    order_created = _poll_until(
        lambda: order_db.orders.find_one({"payment_reference": payment_reference}) is not None,
        timeout_seconds=10
    )
    assert order_created, "Order was not created within 10 seconds after service recovery"
    
    # Assert exactly one order
    orders = list(order_db.orders.find({"payment_reference": payment_reference}))
    assert len(orders) == 1
    
    order = orders[0]
    assert order["email"] == "guest@example.com"
    assert order["ship_to"] == _ship_to()
    assert len(order["lines"]) == 1
    assert order["lines"][0]["product_id"] == "prod_001"
    assert order["lines"][0]["quantity"] == 2
    assert order["lines"][0]["price_minor_units"] == 1500
    assert order["total_minor_units"] == 3000
    
    mongo_client.close()


@pytest.mark.asyncio
async def test_repeated_checkout_does_not_charge_twice():
    """AC4: A repeated checkout does not charge or order twice."""
    cart_id = _create_cart(quantity=1, price=1000)
    
    body = {
        "cart_id": cart_id,
        "email": "guest@example.com",
        "ship_to": _ship_to()
    }
    
    async with httpx.AsyncClient() as client:
        response1 = await client.post(f"{CHECKOUT_URL}/checkout", json=body)
        response2 = await client.post(f"{CHECKOUT_URL}/checkout", json=body)
    
    assert response1.status_code == 200
    assert response2.status_code == 200
    
    checkout_id_1 = response1.json()["checkout_id"]
    checkout_id_2 = response2.json()["checkout_id"]
    
    assert checkout_id_1 == checkout_id_2
    assert response2.json()["status"] == "accepted"
    
    # Only one checkout record
    mongo_client = MongoClient(MONGODB_URI)
    checkout_db = mongo_client["checkout_db"]
    checkout = checkout_db.checkouts.find_one({"checkout_id": checkout_id_1})
    payment_reference = checkout["payment_reference"]
    
    all_checkouts = list(checkout_db.checkouts.find({"payment_reference": payment_reference}))
    assert len(all_checkouts) == 1
    
    # Only one outbox row
    all_outbox = list(checkout_db.outbox.find({"payment_reference": payment_reference}))
    assert len(all_outbox) == 1
    
    # Only one charge
    payment_db = mongo_client["payment_db"]
    all_charges = list(payment_db.charges.find({"reference": payment_reference}))
    assert len(all_charges) == 1
    
    # Wait for order to be created
    order_db = mongo_client["order_db"]
    order_created = _poll_until(
        lambda: order_db.orders.find_one({"payment_reference": payment_reference}) is not None,
        timeout_seconds=10
    )
    assert order_created, "Order was not created within 10 seconds"
    
    # Manually publish the same OrderRequested message twice
    outbox_row = checkout_db.outbox.find_one({"payment_reference": payment_reference})
    msg = outbox_row["payload"]
    
    connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URI))
    channel = connection.channel()
    
    # Publish twice
    for _ in range(2):
        channel.basic_publish(
            exchange="ecom.events",
            routing_key="order.requested",
            body=json.dumps(msg),
            properties=pika.BasicProperties(delivery_mode=2)
        )
    
    channel.close()
    connection.close()
    
    # Wait for order service to consume both messages
    time.sleep(5)
    
    # Assert only one order exists
    all_orders = list(order_db.orders.find({"payment_reference": payment_reference}))
    assert len(all_orders) == 1
    
    mongo_client.close()


@pytest.mark.manual
@pytest.mark.asyncio
async def test_cart_emptied_and_retried():
    """
    AC5: The cart is emptied, and a failed delete is retried.
    
    MANUAL TEST: Requires stopping and starting the cart service.
    1. Run the test
    2. When prompted, stop the cart service
    3. When prompted, start the cart service
    """
    cart_id = _create_cart(quantity=1, price=1000)
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{CHECKOUT_URL}/checkout",
            json={
                "cart_id": cart_id,
                "email": "guest@example.com",
                "ship_to": _ship_to()
            }
        )
    
    assert response.status_code == 200
    checkout_id = response.json()["checkout_id"]
    
    # Wait for order to be created
    mongo_client = MongoClient(MONGODB_URI)
    checkout_db = mongo_client["checkout_db"]
    order_db = mongo_client["order_db"]
    
    checkout = checkout_db.checkouts.find_one({"checkout_id": checkout_id})
    payment_reference = checkout["payment_reference"]
    
    order_created = _poll_until(
        lambda: order_db.orders.find_one({"payment_reference": payment_reference}) is not None,
        timeout_seconds=10
    )
    assert order_created, "Order was not created within 10 seconds"
    
    # Manual step: stop cart service before first delete attempt
    print("Stop cart service manually, then press Enter")
    input()
    
    # Wait for first retry delay (1s) + processing
    time.sleep(2)
    
    # Assert cart still exists
    cart_db = mongo_client["cart_db"]
    cart = cart_db.carts.find_one({"_id": cart_id})
    assert cart is not None, "Cart was deleted despite cart service being down"
    
    # Manual step: start cart service
    print("Start cart service manually, then press Enter")
    input()
    
    # Poll for cart deletion (retry succeeds)
    cart_deleted = _poll_until(
        lambda: cart_db.carts.find_one({"_id": cart_id}) is None,
        timeout_seconds=10
    )
    assert cart_deleted, "Cart was not deleted within 10 seconds after service recovery"
    
    # Assert only one order exists
    all_orders = list(order_db.orders.find({"payment_reference": payment_reference}))
    assert len(all_orders) == 1
    
    mongo_client.close()


@pytest.mark.manual
@pytest.mark.asyncio
async def test_charge_refunded_when_order_never_appears():
    """
    AC6: A charge with no order is refunded.
    
    MANUAL TEST: Requires stopping the order service and setting COMPLETION_WINDOW_SECONDS=5.
    1. Set COMPLETION_WINDOW_SECONDS=5 environment variable before running
    2. Stop the order service before running this test
    """
    # Verify COMPLETION_WINDOW_SECONDS=5 is set
    assert os.environ.get("COMPLETION_WINDOW_SECONDS") == "5", \
        "COMPLETION_WINDOW_SECONDS must be set to 5 for this test"
    
    cart_id = _create_cart(quantity=1, price=1000)
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{CHECKOUT_URL}/checkout",
            json={
                "cart_id": cart_id,
                "email": "guest@example.com",
                "ship_to": _ship_to()
            }
        )
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted"
    checkout_id = data["checkout_id"]
    
    # Wait for relay to publish OrderRequested
    mongo_client = MongoClient(MONGODB_URI)
    checkout_db = mongo_client["checkout_db"]
    checkout = checkout_db.checkouts.find_one({"checkout_id": checkout_id})
    payment_reference = checkout["payment_reference"]
    
    published = _poll_until(
        lambda: checkout_db.outbox.find_one({"payment_reference": payment_reference, "published": True}) is not None,
        timeout_seconds=10
    )
    assert published, "Relay did not publish OrderRequested within 10 seconds"
    
    # Wait for completion window (5s) + buffer (5s)
    time.sleep(10)
    
    # Poll for RefundRequested outbox row
    refund_row_exists = _poll_until(
        lambda: checkout_db.outbox.find_one({
            "payment_reference": payment_reference,
            "event_type": "RefundRequested"
        }) is not None,
        timeout_seconds=10
    )
    assert refund_row_exists, "RefundRequested outbox row was not created"
    
    # Assert RefundRequested is published
    refund_row = checkout_db.outbox.find_one({
        "payment_reference": payment_reference,
        "event_type": "RefundRequested"
    })
    assert refund_row["published"] == True, "RefundRequested was not published"
    
    # Poll for charge refund
    payment_db = mongo_client["payment_db"]
    refunded = _poll_until(
        lambda: payment_db.charges.find_one({
            "reference": payment_reference,
            "status": "refunded"
        }) is not None,
        timeout_seconds=10
    )
    assert refunded, "Charge was not refunded within 10 seconds"
    
    # Verify checkout status is refunded
    async with httpx.AsyncClient() as client:
        status_response = await client.get(f"{CHECKOUT_URL}/checkout/{checkout_id}")
    
    assert status_response.status_code == 200
    status_data = status_response.json()
    assert status_data["status"] in ["refunded", "refund_pending"]
    
    # Assert no order exists
    order_db = mongo_client["order_db"]
    assert order_db.orders.count_documents({"payment_reference": payment_reference}) == 0
    
    mongo_client.close()


@pytest.mark.asyncio
async def test_checkout_status_endpoint():
    """AC7: The client can see how the checkout finished."""
    cart_id = _create_cart(quantity=1, price=1000)
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{CHECKOUT_URL}/checkout",
            json={
                "cart_id": cart_id,
                "email": "guest@example.com",
                "ship_to": _ship_to()
            }
        )
    
    assert response.status_code == 200
    checkout_id = response.json()["checkout_id"]
    
    # Immediately fetch status
    async with httpx.AsyncClient() as client:
        status_response = await client.get(f"{CHECKOUT_URL}/checkout/{checkout_id}")
    
    assert status_response.status_code == 200
    status_data = status_response.json()
    assert status_data["checkout_id"] == checkout_id
    assert status_data["status"] == "accepted"
    assert status_data.get("order_id") is None
    
    # Wait for order to be created
    mongo_client = MongoClient(MONGODB_URI)
    checkout_db = mongo_client["checkout_db"]
    order_db = mongo_client["order_db"]
    
    checkout = checkout_db.checkouts.find_one({"checkout_id": checkout_id})
    payment_reference = checkout["payment_reference"]
    
    order_created = _poll_until(
        lambda: order_db.orders.find_one({"payment_reference": payment_reference}) is not None,
        timeout_seconds=10
    )
    assert order_created, "Order was not created within 10 seconds"
    
    # Fetch status again
    async with httpx.AsyncClient() as client:
        status_response = await client.get(f"{CHECKOUT_URL}/checkout/{checkout_id}")
    
    assert status_response.status_code == 200
    status_data = status_response.json()
    assert status_data["status"] == "order_created"
    assert status_data.get("order_id") is not None
    order_id = status_data["order_id"]
    
    # Wait for cart to be deleted
    cart_db = mongo_client["cart_db"]
    cart_deleted = _poll_until(
        lambda: cart_db.carts.find_one({"_id": cart_id}) is None,
        timeout_seconds=10
    )
    assert cart_deleted, "Cart was not deleted within 10 seconds"
    
    # Fetch status final time
    async with httpx.AsyncClient() as client:
        status_response = await client.get(f"{CHECKOUT_URL}/checkout/{checkout_id}")
    
    assert status_response.status_code == 200
    status_data = status_response.json()
    assert status_data["status"] == "cart_cleared"
    assert status_data["order_id"] == order_id
    
    mongo_client.close()


@pytest.mark.asyncio
async def test_database_isolation():
    """AC8: Services do not share databases."""
    cart_id = _create_cart(quantity=1, price=1000)
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{CHECKOUT_URL}/checkout",
            json={
                "cart_id": cart_id,
                "email": "guest@example.com",
                "ship_to": _ship_to()
            }
        )
    
    assert response.status_code == 200
    checkout_id = response.json()["checkout_id"]
    
    # Wait for order to be created and cart to be deleted
    mongo_client = MongoClient(MONGODB_URI)
    checkout_db = mongo_client["checkout_db"]
    order_db = mongo_client["order_db"]
    cart_db = mongo_client["cart_db"]
    
    checkout = checkout_db.checkouts.find_one({"checkout_id": checkout_id})
    payment_reference = checkout["payment_reference"]
    
    order_created = _poll_until(
        lambda: order_db.orders.find_one({"payment_reference": payment_reference}) is not None,
        timeout_seconds=10
    )
    assert order_created, "Order was not created within 10 seconds"
    
    cart_deleted = _poll_until(
        lambda: cart_db.carts.find_one({"_id": cart_id}) is None,
        timeout_seconds=10
    )
    assert cart_deleted, "Cart was not deleted within 10 seconds"
    
    # Inspect checkout_db
    checkout_collections = checkout_db.list_collection_names()
    assert set(checkout_collections) == {"checkouts", "outbox"}
    
    # Inspect order_db
    order_collections = order_db.list_collection_names()
    assert set(order_collections) == {"orders"}
    
    # Inspect payment_db
    payment_db = mongo_client["payment_db"]
    payment_collections = payment_db.list_collection_names()
    assert set(payment_collections) == {"charges"}
    
    # Inspect cart_db
    cart_collections = cart_db.list_collection_names()
    # Cart collection may still exist (empty after delete) or may be dropped
    if "carts" in cart_collections:
        assert cart_db.carts.count_documents({}) == 0
    
    # Assert no cross-database writes (implicit from above assertions)
    mongo_client.close()
