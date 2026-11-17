import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx

import consumers
import main
from db import checkouts_collection

SERVICE_ROOT = Path(__file__).resolve().parents[1]
OTHER_DATABASES = ("order_db", "payment_db", "cart_db", "identity_db", "catalog_db")

checkouts_data: dict = {}
carts: dict = {}
delete_calls: list = []
delete_results: list = []
status_updates: list = []


class _Response:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _DeleteClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def delete(self, url):
        delete_calls.append(url)
        result = delete_results.pop(0) if delete_results else 204
        if isinstance(result, Exception):
            raise result
        if result in (204, 404):
            cart_id = url.rstrip("/").rsplit("/", 1)[-1]
            carts.pop(cart_id, None)
        return _Response(result)


def setup_function():
    checkouts_data.clear()
    carts.clear()
    delete_calls.clear()
    delete_results.clear()
    status_updates.clear()
    consumers.httpx.AsyncClient = _DeleteClient

    def find_one(query):
        if "payment_reference" in query:
            return checkouts_data.get(query["payment_reference"])
        return None

    def update_one(query, update):
        checkout = checkouts_data.get(query["payment_reference"])
        if checkout is not None:
            fields = update.get("$set", {})
            checkout.update(fields)
            if "status" in fields:
                status_updates.append(fields["status"])
        return None

    checkouts_collection.find_one.side_effect = find_one
    checkouts_collection.update_one.side_effect = update_one


def _ship_to():
    return {
        "street": "123 Main St",
        "city": "Springfield",
        "postal_code": "12345",
        "country": "US",
    }


def _seed_checkout(payment_reference, cart_id, checkout_id, total=3000):
    now = datetime.utcnow()
    checkouts_data[payment_reference] = {
        "checkout_id": checkout_id,
        "payment_reference": payment_reference,
        "cart_id": cart_id,
        "email": "guest@example.com",
        "ship_to": _ship_to(),
        "lines": [
            {
                "product_id": "prod_001",
                "quantity": 2,
                "price_snapshot_minor_units": total // 2,
            }
        ],
        "total_minor_units": total,
        "status": "accepted",
        "order_id": None,
        "created_at": now,
        "updated_at": now,
    }
    carts[cart_id] = {
        "cart_id": cart_id,
        "lines": [
            {
                "product_id": "prod_001",
                "quantity": 2,
                "price_snapshot_minor_units": total // 2,
            }
        ],
        "total_minor_units": total,
    }


def _order_created(payment_reference, checkout_id, order_id):
    return {
        "event_type": "OrderCreated",
        "payment_reference": payment_reference,
        "checkout_id": checkout_id,
        "order_id": order_id,
        "timestamp": datetime.utcnow().isoformat(),
    }


def _deliver(payload, delivery_tag=1):
    channel = SimpleNamespace(acked=[], nacked=[])

    def basic_ack(delivery_tag):
        channel.acked.append(delivery_tag)

    def basic_nack(delivery_tag, requeue=True):
        channel.nacked.append((delivery_tag, requeue))

    channel.basic_ack = basic_ack
    channel.basic_nack = basic_nack
    method = SimpleNamespace(delivery_tag=delivery_tag)
    consumers.on_message(channel, method, None, json.dumps(payload).encode())
    return channel


def test_cart_deleted_on_order_created():
    """AC5: Cart is deleted after OrderCreated is received (happy path)."""
    _seed_checkout("pay_test_1", "cart_test_1", "chk_test_1")
    channel = _deliver(_order_created("pay_test_1", "chk_test_1", "ord_test_1"))

    checkout = checkouts_data["pay_test_1"]
    assert checkout["status"] == "cart_cleared"
    assert checkout["order_id"] == "ord_test_1"
    assert "cart_test_1" not in carts
    assert delete_calls == ["http://localhost:8003/cart/cart_test_1"]
    assert status_updates == ["order_created", "cart_cleared"]
    assert channel.acked == [1]
    assert channel.nacked == []


def test_cart_delete_retried_on_failure(monkeypatch):
    """AC5: Cart delete is retried on failure, then the cart is cleared."""
    slept = []

    async def instant(delay):
        slept.append(delay)

    monkeypatch.setattr(consumers.asyncio, "sleep", instant)
    _seed_checkout("pay_retry", "cart_retry", "chk_retry", total=1000)
    delete_results.extend([500, 204])

    channel = _deliver(_order_created("pay_retry", "chk_retry", "ord_retry"))

    checkout = checkouts_data["pay_retry"]
    assert checkout["status"] == "cart_cleared"
    assert checkout["order_id"] == "ord_retry"
    assert "cart_retry" not in carts
    assert len(delete_calls) == 2
    assert slept == [5]
    assert channel.acked == [1]


def test_cart_delete_retry_exhausted(capsys, monkeypatch):
    """AC5: Exhausted cart-delete retries leave the checkout at order_created."""
    slept = []

    async def instant(delay):
        slept.append(delay)

    monkeypatch.setattr(consumers.asyncio, "sleep", instant)
    _seed_checkout("pay_exhaust", "cart_exhaust", "chk_exhaust", total=1000)
    delete_results.extend([500, 503, 500, 500])

    channel = _deliver(_order_created("pay_exhaust", "chk_exhaust", "ord_exhaust"))

    checkout = checkouts_data["pay_exhaust"]
    assert checkout["status"] == "order_created"
    assert checkout["order_id"] == "ord_exhaust"
    assert "cart_exhaust" in carts
    assert len(delete_calls) == 4
    assert slept == [5, 10, 20]
    assert channel.acked == [1]
    logged = capsys.readouterr().out
    assert "Cart delete exhausted all retries for cart cart_exhaust" in logged
    assert "[KNOWN_GAP] Cart delete failed for cart cart_exhaust" in logged


def test_duplicate_order_created_message(monkeypatch):
    """AC5: A duplicate OrderCreated does not clear the cart or rewrite the checkout twice."""
    async def instant(_delay):
        return None

    monkeypatch.setattr(consumers.asyncio, "sleep", instant)
    _seed_checkout("pay_dup", "cart_dup", "chk_dup", total=1000)

    first = _deliver(_order_created("pay_dup", "chk_dup", "ord_dup"), delivery_tag=1)
    second = _deliver(_order_created("pay_dup", "chk_dup", "ord_other"), delivery_tag=2)

    checkout = checkouts_data["pay_dup"]
    assert checkout["status"] == "cart_cleared"
    assert checkout["order_id"] == "ord_dup"
    assert "cart_dup" not in carts
    assert delete_calls == ["http://localhost:8003/cart/cart_dup"]
    assert status_updates == ["order_created", "cart_cleared"]
    assert first.acked == [1]
    assert second.acked == [2]


def test_database_isolation_checkout_consumer(monkeypatch):
    """AC8: The OrderCreated consumer only reads and writes checkout_db."""
    _seed_checkout("pay_iso", "cart_iso", "chk_iso", total=1000)
    _deliver(_order_created("pay_iso", "chk_iso", "ord_iso"))

    assert checkouts_data["pay_iso"]["status"] == "cart_cleared"
    assert "cart_iso" not in carts

    source = (SERVICE_ROOT / "consumers.py").read_text()
    for db_name in OTHER_DATABASES:
        assert db_name not in source, f"{db_name} found in consumers.py"
    assert "checkouts_collection" in source

    started = {}

    async def fake_consume():
        started["called"] = True

    monkeypatch.setattr(main, "consume_order_created", fake_consume)
    startup = next(
        handler
        for handler in main.app.router.on_startup
        if getattr(handler, "__name__", "") == "startup_event"
    )

    async def run_startup():
        await startup()
        await asyncio.sleep(0)

    asyncio.run(run_startup())
    assert started["called"] is True


def test_consumer_binds_order_created_queue(monkeypatch):
    """The consumer listens on order.created.queue with manual acknowledgement."""
    channel = MagicMock()
    connection = MagicMock()
    connection.channel.return_value = channel
    monkeypatch.setattr(consumers, "get_rabbitmq_connection", lambda: connection)

    consumers._consume_blocking()

    channel.exchange_declare.assert_called_once_with(
        exchange="ecom.events", exchange_type="topic", durable=True
    )
    channel.queue_declare.assert_called_once_with(
        queue="order.created.queue", durable=True
    )
    channel.queue_bind.assert_called_once_with(
        exchange="ecom.events",
        queue="order.created.queue",
        routing_key="order.created",
    )
    channel.basic_qos.assert_called_once_with(prefetch_count=1)
    consume = channel.basic_consume.call_args
    assert consume.kwargs["queue"] == "order.created.queue"
    assert consume.kwargs["on_message_callback"] is consumers.on_message
    assert consume.kwargs["auto_ack"] is False
    channel.start_consuming.assert_called_once()


def test_cart_delete_retries_network_error(monkeypatch):
    """A connection error is retried, and a later 204 clears the cart."""
    slept = []

    async def instant(delay):
        slept.append(delay)

    monkeypatch.setattr(consumers.asyncio, "sleep", instant)
    _seed_checkout("pay_net", "cart_net", "chk_net", total=1000)
    delete_results.extend([httpx.ConnectError("connection refused"), 204])

    _deliver(_order_created("pay_net", "chk_net", "ord_net"))

    assert checkouts_data["pay_net"]["status"] == "cart_cleared"
    assert "cart_net" not in carts
    assert len(delete_calls) == 2
    assert slept == [5]
