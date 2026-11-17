import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from pymongo.errors import PyMongoError

import consumers
import main
from db import charges_collection

SERVICE_ROOT = Path(__file__).resolve().parents[1]
OTHER_DATABASES = (
    "checkout_db",
    "order_db",
    "cart_db",
    "identity_db",
    "catalog_db",
)

charges: dict = {}


def setup_function():
    charges.clear()
    charges_collection.find_one.reset_mock()
    charges_collection.update_one.reset_mock()

    def find_one(query):
        return charges.get(query["reference"])

    def update_one(query, update):
        charge = charges.get(query["reference"])
        if charge is not None:
            charge.update(update.get("$set", {}))
        return None

    charges_collection.find_one.side_effect = find_one
    charges_collection.update_one.side_effect = update_one


def _seed(reference, status="accepted"):
    charges[reference] = {
        "reference": reference,
        "amount_minor_units": 1000,
        "status": status,
        "timestamp": datetime.utcnow(),
    }


def _message(**overrides):
    payload = {
        "event_type": "RefundRequested",
        "payment_reference": "pay_expired",
        "checkout_id": "chk_expired",
        "timestamp": datetime.utcnow().isoformat(),
    }
    payload.update(overrides)
    return payload


def _deliver(payload, delivery_tag=1):
    channel = SimpleNamespace(acked=[], nacked=[])

    def basic_ack(delivery_tag):
        channel.acked.append(delivery_tag)

    def basic_nack(delivery_tag, requeue=True):
        channel.nacked.append((delivery_tag, requeue))

    channel.basic_ack = basic_ack
    channel.basic_nack = basic_nack
    method = SimpleNamespace(delivery_tag=delivery_tag)
    body = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode()
    consumers.on_message(channel, method, None, body)
    return channel


def test_charge_refunded_after_completion_window():
    """AC6: A charge with no order is refunded."""
    _seed("pay_expired")
    channel = _deliver(_message())

    charge = charges["pay_expired"]
    assert charge["status"] == "refunded"
    assert charge["refunded_at"] is not None
    assert isinstance(charge["refunded_at"], datetime)
    assert "checkout_id" not in charge
    assert channel.acked == [1]
    assert channel.nacked == []


def test_refund_idempotency():
    """AC6: Duplicate RefundRequested message does not refund twice."""
    _seed("pay_dup")
    msg = _message(payment_reference="pay_dup", checkout_id="chk_dup")

    first = _deliver(msg, delivery_tag=1)
    refunded_at = charges["pay_dup"]["refunded_at"]
    second = _deliver(msg, delivery_tag=2)

    charge = charges["pay_dup"]
    assert charge["status"] == "refunded"
    assert charge["refunded_at"] == refunded_at
    assert first.acked == [1]
    assert second.acked == [2]
    assert charges_collection.update_one.call_count == 1


def test_no_order_created_after_refund():
    """AC6: No order is created after refund."""
    _seed("pay_no_order")
    channel = _deliver(
        _message(payment_reference="pay_no_order", checkout_id="chk_no_order")
    )

    charge = charges["pay_no_order"]
    assert charge["status"] == "refunded"
    assert charge["refunded_at"] is not None
    assert "order_id" not in charge

    source = (SERVICE_ROOT / "consumers.py").read_text()
    assert "order_db" not in source
    assert "orders_collection" not in source
    assert channel.acked == [1]


def test_database_isolation_payment_consumer(monkeypatch):
    """AC8: Payment consumer only accesses payment_db."""
    _seed("pay_iso")
    channel = _deliver(_message(payment_reference="pay_iso", checkout_id="chk_iso"))

    assert charges["pay_iso"]["status"] == "refunded"
    assert channel.acked == [1]

    source = (SERVICE_ROOT / "consumers.py").read_text()
    for db_name in OTHER_DATABASES:
        assert db_name not in source, f"{db_name} found in consumers.py"
    assert "charges_collection" in source

    started = {}

    async def fake_consume():
        started["called"] = True

    monkeypatch.setattr(main, "consume_refund_requested", fake_consume)
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


def test_consumer_binds_refund_requested_queue(monkeypatch):
    """The consumer listens on payment.refund.requested.queue with manual acknowledgement."""
    channel = MagicMock()
    connection = MagicMock()
    connection.channel.return_value = channel
    monkeypatch.setattr(consumers, "get_rabbitmq_connection", lambda: connection)

    consumers._consume_blocking()

    channel.exchange_declare.assert_called_once_with(
        exchange="ecom.events", exchange_type="topic", durable=True
    )
    channel.queue_declare.assert_called_once_with(
        queue="payment.refund.requested.queue", durable=True
    )
    channel.queue_bind.assert_called_once_with(
        exchange="ecom.events",
        queue="payment.refund.requested.queue",
        routing_key="payment.refund.requested",
    )
    channel.basic_qos.assert_called_once_with(prefetch_count=1)
    consume = channel.basic_consume.call_args
    assert consume.kwargs["queue"] == "payment.refund.requested.queue"
    assert consume.kwargs["on_message_callback"] is consumers.on_message
    assert consume.kwargs["auto_ack"] is False
    channel.start_consuming.assert_called_once()


def test_invalid_refund_requested_is_nacked():
    channel = _deliver(b"not-json")
    assert channel.acked == []
    assert channel.nacked == [(1, False)]

    channel = _deliver({"event_type": "RefundRequested", "payment_reference": ""})
    assert channel.nacked == [(1, False)]
    assert charges == {}


def test_orphaned_refund_requested_is_acked():
    channel = _deliver(_message(payment_reference="pay_missing"))
    assert channel.acked == [1]
    assert channel.nacked == []
    assert charges_collection.update_one.call_count == 0


def test_refund_database_error_requeues():
    _seed("pay_db")

    def fail(query, update):
        raise PyMongoError("write failed")

    charges_collection.update_one.side_effect = fail
    channel = _deliver(_message(payment_reference="pay_db", checkout_id="chk_db"))

    assert charges["pay_db"]["status"] == "accepted"
    assert "refunded_at" not in charges["pay_db"]
    assert channel.acked == []
    assert channel.nacked == [(1, True)]
