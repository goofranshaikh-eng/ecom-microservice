import copy
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import httpx
from bson import ObjectId
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pymongo.errors import PyMongoError

import completion_checker
import relay
import routes
from db import checkouts_collection
from db import client as mongo_client
from db import db as checkout_db
from db import outbox_collection
from main import app
from models import CheckoutStatusResponse

client = TestClient(app)
SERVICE_ROOT = Path(__file__).resolve().parents[1]
OTHER_DATABASES = (
    "identity_db",
    "catalog_db",
    "cart_db",
    "payment_db",
    "order_db",
)

carts: dict = {}
orders: dict = {}
calls: list = []
checkouts_data: dict = {}
outbox_data: dict = {}
order_status_code = 201
delete_status_code = 204
payment_status_code = 200
cart_status_code = None


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url):
        calls.append(("GET", url, None))
        if cart_status_code is not None:
            return _FakeResponse(cart_status_code, {})
        cart_id = url.rstrip("/").rsplit("/", 1)[-1]
        cart = carts.get(cart_id)
        if cart is None:
            return _FakeResponse(404, {})
        return _FakeResponse(200, cart)

    async def post(self, url, json=None):
        calls.append(("POST", url, json))
        if url.endswith("/charge"):
            if payment_status_code != 200:
                return _FakeResponse(payment_status_code, {})
            if json is None or "payment_reference" not in json:
                return _FakeResponse(400, {"error": "payment_reference required"})
            amount = json["amount_minor_units"]
            if amount >= 100000:
                return _FakeResponse(
                    200, {"status": "refused", "reason": "Amount exceeds limit"}
                )
            return _FakeResponse(
                200, {"status": "accepted", "reference": json["payment_reference"]}
            )
        if url.endswith("/orders"):
            if order_status_code != 201:
                return _FakeResponse(order_status_code, {})
            order_id = "order-1"
            orders[order_id] = json
            return _FakeResponse(201, {"order_id": order_id})
        return _FakeResponse(500, {})

    async def delete(self, url):
        calls.append(("DELETE", url, None))
        if delete_status_code != 204:
            return _FakeResponse(delete_status_code, {})
        cart_id = url.rstrip("/").rsplit("/", 1)[-1]
        carts.pop(cart_id, None)
        return _FakeResponse(204, {})


def setup_function():
    global order_status_code, delete_status_code, payment_status_code, cart_status_code
    carts.clear()
    orders.clear()
    calls.clear()
    checkouts_data.clear()
    outbox_data.clear()
    order_status_code = 201
    delete_status_code = 204
    payment_status_code = 200
    cart_status_code = None
    routes.httpx.AsyncClient = _FakeAsyncClient

    def insert_one_checkouts(doc):
        checkout_id = doc["checkout_id"]
        checkouts_data[checkout_id] = {**doc}
        return MagicMock(inserted_id=ObjectId())

    def find_one_checkouts(query):
        if "checkout_id" in query:
            return checkouts_data.get(query["checkout_id"])
        if "cart_id" in query:
            for checkout in checkouts_data.values():
                if checkout.get("cart_id") == query["cart_id"]:
                    if "status" in query:
                        if checkout.get("status") in query["status"].get("$in", []):
                            return checkout
                    else:
                        return checkout
        if "payment_reference" in query:
            for checkout in checkouts_data.values():
                if checkout.get("payment_reference") == query["payment_reference"]:
                    return checkout
        return None

    def update_one_checkouts(query, update):
        if "checkout_id" in query:
            checkout = checkouts_data.get(query["checkout_id"])
            if checkout:
                checkout.update(update.get("$set", {}))
        return MagicMock(matched_count=1)

    def delete_one_checkouts(query):
        if "checkout_id" in query:
            checkouts_data.pop(query["checkout_id"], None)
        return MagicMock(deleted_count=1)

    def insert_one_outbox(doc):
        row_id = str(ObjectId())
        outbox_data[row_id] = {**doc, "_id": ObjectId(row_id)}
        return MagicMock(inserted_id=ObjectId(row_id))

    def find_one_outbox(query):
        if "payment_reference" in query:
            for row in outbox_data.values():
                if row.get("payment_reference") == query["payment_reference"]:
                    if "event_type" in query:
                        if row.get("event_type") == query["event_type"]:
                            return row
                    else:
                        return row
        return None

    def delete_many_checkouts(_query):
        checkouts_data.clear()
        return MagicMock(deleted_count=0)

    checkouts_collection.insert_one.side_effect = insert_one_checkouts
    checkouts_collection.find_one.side_effect = find_one_checkouts
    checkouts_collection.update_one.side_effect = update_one_checkouts
    checkouts_collection.delete_one.side_effect = delete_one_checkouts
    checkouts_collection.delete_many.side_effect = delete_many_checkouts
    checkouts_collection.find.side_effect = None
    checkouts_collection.find.return_value = MagicMock(name="checkouts_find")

    outbox_collection.insert_one.side_effect = insert_one_outbox
    outbox_collection.find_one.side_effect = find_one_outbox
    outbox_collection.find.side_effect = None
    outbox_collection.find.return_value = MagicMock(name="outbox_find")
    outbox_collection.update_one.side_effect = None


def _ship_to(street="123 Main St", city="Springfield", postal_code="12345"):
    return {
        "street": street,
        "city": city,
        "postal_code": postal_code,
        "country": "USA",
    }


def _seed_cart(cart_id: str, quantity: int, price: int = 1000) -> dict:
    cart = {
        "cart_id": cart_id,
        "lines": [
            {
                "product_id": "prod1",
                "quantity": quantity,
                "price_snapshot_minor_units": price,
            }
        ],
        "total_minor_units": price * quantity,
    }
    carts[cart_id] = cart
    return cart


def test_guest_checkout_success():
    cart_id = "cart-guest"
    _seed_cart(cart_id, quantity=2)
    ship_to = _ship_to()

    response = client.post(
        "/checkout",
        json={
            "cart_id": cart_id,
            "email": "guest@example.com",
            "ship_to": ship_to,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert "order_id" not in body
    uuid.UUID(body["checkout_id"])
    assert cart_id in carts
    checkout = checkouts_collection.find_one({"checkout_id": body["checkout_id"]})
    assert checkout["status"] == "accepted"
    assert checkout["email"] == "guest@example.com"
    assert checkout["ship_to"] == ship_to
    assert checkout["total_minor_units"] == 2000
    assert checkout["lines"] == [
        {
            "product_id": "prod1",
            "quantity": 2,
            "price_snapshot_minor_units": 1000,
        }
    ]
    payment_reference = checkout["payment_reference"]
    assert calls[0] == ("GET", f"http://localhost:8003/cart/{cart_id}", None)
    assert calls[1] == (
        "POST",
        "http://localhost:8005/charge",
        {"amount_minor_units": 2000, "payment_reference": payment_reference},
    )
    outbox_row = outbox_collection.find_one({"payment_reference": payment_reference})
    assert outbox_row["event_type"] == "OrderRequested"
    assert outbox_row["published"] is False
    assert outbox_row["payload"]["email"] == "guest@example.com"
    assert outbox_row["payload"]["ship_to"] == ship_to
    assert outbox_row["payload"]["lines"] == [
        {"product_id": "prod1", "quantity": 2, "price_minor_units": 1000}
    ]
    assert outbox_row["payload"]["total_minor_units"] == 2000
    assert outbox_row["payload"]["payment_reference"] == payment_reference
    assert not any(call[1].endswith("/orders") for call in calls)
    assert not any(call[0] == "DELETE" for call in calls)


def test_signed_in_checkout_success():
    cart_id = "cart-shopper"
    _seed_cart(cart_id, quantity=1)
    ship_to = _ship_to(street="456 Elm St", city="Shelbyville", postal_code="67890")

    response = client.post(
        "/checkout",
        json={
            "cart_id": cart_id,
            "shopper_id": "shopper-1",
            "ship_to": ship_to,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert "order_id" not in body
    assert cart_id in carts
    checkout = checkouts_collection.find_one({"checkout_id": body["checkout_id"]})
    assert checkout["shopper_id"] == "shopper-1"
    assert checkout["email"] is None
    assert checkout["ship_to"] == ship_to
    assert checkout["lines"] == [
        {
            "product_id": "prod1",
            "quantity": 1,
            "price_snapshot_minor_units": 1000,
        }
    ]
    assert checkout["total_minor_units"] == 1000
    assert calls[1][2]["amount_minor_units"] == 1000
    outbox_row = outbox_collection.find_one(
        {"payment_reference": checkout["payment_reference"]}
    )
    assert outbox_row["payload"]["shopper_id"] == "shopper-1"
    assert outbox_row["payload"]["email"] is None
    assert outbox_row["payload"]["payment_reference"] == checkout["payment_reference"]


def test_payment_refused():
    cart_id = "cart-refused"
    _seed_cart(cart_id, quantity=200)
    before = copy.deepcopy(carts[cart_id])

    response = client.post(
        "/checkout",
        json={
            "cart_id": cart_id,
            "email": "guest@example.com",
            "ship_to": _ship_to(),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "refused"
    assert body["reason"] == "Amount exceeds limit"
    uuid.UUID(body["checkout_id"])
    checkout = checkouts_collection.find_one({"checkout_id": body["checkout_id"]})
    assert checkout["status"] == "refused"
    assert (
        outbox_collection.find_one(
            {"payment_reference": checkout["payment_reference"]}
        )
        is None
    )
    assert carts[cart_id] == before
    assert orders == {}
    assert not any(call[0] == "DELETE" for call in calls)
    assert not any(call[1].endswith("/orders") for call in calls)
    assert calls[1][2]["amount_minor_units"] == 200000
    assert calls[1][2]["payment_reference"] == checkout["payment_reference"]


def test_cart_unchanged_on_payment_refusal():
    cart_id = "cart-unchanged"
    _seed_cart(cart_id, quantity=200)
    before = copy.deepcopy(carts[cart_id])

    response = client.post(
        "/checkout",
        json={
            "cart_id": cart_id,
            "email": "guest@example.com",
            "ship_to": _ship_to(),
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "refused"
    assert carts[cart_id] == before
    assert carts[cart_id]["lines"][0]["quantity"] == 200
    assert carts[cart_id]["total_minor_units"] == 200000


def test_cart_remains_on_accepted_checkout():
    cart_id = "cart-emptied"
    _seed_cart(cart_id, quantity=2)

    response = client.post(
        "/checkout",
        json={
            "cart_id": cart_id,
            "email": "guest@example.com",
            "ship_to": _ship_to(),
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert cart_id in carts


def test_outbox_carries_order_fields_on_payment_success():
    cart_id = "cart-order"
    ship_to = _ship_to()
    _seed_cart(cart_id, quantity=2)

    response = client.post(
        "/checkout",
        json={
            "cart_id": cart_id,
            "email": "guest@example.com",
            "ship_to": ship_to,
        },
    )

    assert response.status_code == 200
    assert "order_id" not in response.json()
    checkout = checkouts_collection.find_one(
        {"checkout_id": response.json()["checkout_id"]}
    )
    outbox_row = outbox_collection.find_one(
        {"payment_reference": checkout["payment_reference"]}
    )
    payload = outbox_row["payload"]
    assert payload["email"] == "guest@example.com"
    assert payload["ship_to"] == ship_to
    assert payload["lines"][0]["product_id"] == "prod1"
    assert payload["lines"][0]["quantity"] == 2
    assert payload["lines"][0]["price_minor_units"] == 1000
    assert payload["total_minor_units"] == 2000
    assert payload["payment_reference"] == checkout["payment_reference"]
    assert orders == {}


def test_swagger_ui_lists_checkout_endpoint():
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "Checkout Service" in docs.text

    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    schema = spec.json()
    assert schema["info"]["title"] == "Checkout Service"
    checkout_post = schema["paths"]["/checkout"]["post"]
    assert "CheckoutRequest" in str(checkout_post["requestBody"])
    assert "CheckoutCreateResponse" in str(checkout_post["responses"]["200"])
    schemas = schema["components"]["schemas"]
    assert "CheckoutRequest" in schemas
    assert "CheckoutCreateResponse" in schemas
    assert "ShipTo" in schemas
    response_schema = schemas["CheckoutCreateResponse"]
    assert "accepted" in str(response_schema)
    assert "refused" in str(response_schema)
    assert "checkout_id" in response_schema["properties"]


def test_guest_email_required():
    response = client.post(
        "/checkout",
        json={"cart_id": "cart-guest", "ship_to": _ship_to()},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Email is required for guest checkout"}
    assert calls == []


def test_cart_not_found():
    response = client.post(
        "/checkout",
        json={
            "cart_id": "missing",
            "email": "guest@example.com",
            "ship_to": _ship_to(),
        },
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Cart not found"}
    assert orders == {}
    assert checkouts_data == {}


def test_downstream_unavailable():
    global cart_status_code, payment_status_code
    cart_id = "cart-down"
    _seed_cart(cart_id, quantity=1)
    body = {
        "cart_id": cart_id,
        "email": "guest@example.com",
        "ship_to": _ship_to(),
    }

    cart_status_code = 503
    cart_down = client.post("/checkout", json=body)
    assert cart_down.status_code == 500
    assert cart_down.json() == {"detail": "Cart service unavailable"}
    assert checkouts_data == {}

    cart_status_code = None
    payment_status_code = 503
    payment_down = client.post("/checkout", json=body)
    assert payment_down.status_code == 500
    assert payment_down.json() == {"detail": "Payment service unavailable"}
    assert cart_id in carts
    assert checkouts_data == {}


def test_charge_uses_returned_cart_total():
    cart_id = "cart-total"
    carts[cart_id] = {
        "cart_id": cart_id,
        "lines": [
            {
                "product_id": "prod1",
                "quantity": 2,
                "price_snapshot_minor_units": 1000,
            }
        ],
        "total_minor_units": 1500,
    }

    response = client.post(
        "/checkout",
        json={
            "cart_id": cart_id,
            "email": "guest@example.com",
            "ship_to": _ship_to(),
        },
    )

    assert response.status_code == 200
    assert calls[1][2]["amount_minor_units"] == 1500
    checkout = checkouts_collection.find_one(
        {"checkout_id": response.json()["checkout_id"]}
    )
    assert checkout["total_minor_units"] == 1500
    outbox_row = outbox_collection.find_one(
        {"payment_reference": checkout["payment_reference"]}
    )
    assert outbox_row["payload"]["total_minor_units"] == 1500


def test_database_isolation():
    cart_id = "cart-iso"
    _seed_cart(cart_id, quantity=1)
    response = client.post(
        "/checkout",
        json={
            "cart_id": cart_id,
            "email": "guest@example.com",
            "ship_to": _ship_to(),
        },
    )
    assert response.status_code == 200

    accessed_databases = [
        call.args[0] for call in mongo_client.__getitem__.call_args_list
    ]
    accessed_collections = [
        call.args[0] for call in checkout_db.__getitem__.call_args_list
    ]
    assert accessed_databases == ["checkout_db"]
    assert set(accessed_collections) == {"checkouts", "outbox"}

    for path in SERVICE_ROOT.glob("*.py"):
        source = path.read_text()
        for name in OTHER_DATABASES:
            assert name not in source


def test_transport_error_is_unavailable():
    class _DownClient(_FakeAsyncClient):
        async def get(self, url):
            raise httpx.RequestError("connection refused")

    routes.httpx.AsyncClient = _DownClient
    response = client.post(
        "/checkout",
        json={
            "cart_id": "cart-down",
            "email": "guest@example.com",
            "ship_to": _ship_to(),
        },
    )
    assert response.status_code == 500
    assert response.json() == {"detail": "Cart service unavailable"}


def test_accepted_checkout_returns_before_order_exists():
    """AC2: Accepted checkout returns before the order exists."""
    cart_id = "cart-1"
    _seed_cart(cart_id, quantity=2, price=1000)

    response = client.post(
        "/checkout",
        json={
            "cart_id": cart_id,
            "email": "guest@example.com",
            "ship_to": _ship_to(),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert "checkout_id" in body
    assert "order_id" not in body

    checkout_id = body["checkout_id"]
    uuid.UUID(checkout_id)

    checkout = checkouts_collection.find_one({"checkout_id": checkout_id})
    assert checkout is not None
    assert checkout["status"] == "accepted"
    assert "payment_reference" in checkout
    assert checkout["total_minor_units"] == 2000
    assert len(checkout["lines"]) == 1
    assert checkout["lines"][0]["product_id"] == "prod1"

    payment_reference = checkout["payment_reference"]

    outbox_row = outbox_collection.find_one({"payment_reference": payment_reference})
    assert outbox_row is not None
    assert outbox_row["event_type"] == "OrderRequested"
    assert outbox_row["published"] is False
    assert outbox_row["payload"]["checkout_id"] == checkout_id
    assert outbox_row["payload"]["total_minor_units"] == 2000

    assert not any(call[1].endswith("/orders") for call in calls)


def test_checkout_and_outbox_inserted_atomically():
    """AC2: Checkout and outbox inserted atomically (implicit check)."""
    cart_id = "cart-atomic"
    _seed_cart(cart_id, quantity=1, price=1000)

    response = client.post(
        "/checkout",
        json={
            "cart_id": cart_id,
            "email": "guest@example.com",
            "ship_to": _ship_to(),
        },
    )

    assert response.status_code == 200
    checkout_id = response.json()["checkout_id"]

    checkout = checkouts_collection.find_one({"checkout_id": checkout_id})
    assert checkout is not None

    payment_reference = checkout["payment_reference"]
    outbox_row = outbox_collection.find_one({"payment_reference": payment_reference})
    assert outbox_row is not None


def test_repeated_checkout_returns_existing_checkout():
    """AC4: A repeated checkout does not charge twice."""
    cart_id = "cart-repeat"
    _seed_cart(cart_id, quantity=2, price=1500)

    body = {
        "cart_id": cart_id,
        "email": "guest@example.com",
        "ship_to": _ship_to(),
    }

    response1 = client.post("/checkout", json=body)
    assert response1.status_code == 200
    checkout_id_1 = response1.json()["checkout_id"]

    response2 = client.post("/checkout", json=body)
    assert response2.status_code == 200
    checkout_id_2 = response2.json()["checkout_id"]

    assert checkout_id_1 == checkout_id_2
    assert response2.json()["status"] == "accepted"

    checkout = checkouts_collection.find_one({"checkout_id": checkout_id_1})
    payment_reference = checkout["payment_reference"]
    all_checkouts = [
        c
        for c in checkouts_data.values()
        if c.get("payment_reference") == payment_reference
    ]
    assert len(all_checkouts) == 1

    all_outbox = [
        r for r in outbox_data.values() if r.get("payment_reference") == payment_reference
    ]
    assert len(all_outbox) == 1

    payment_calls = [call for call in calls if call[1].endswith("/charge")]
    assert len(payment_calls) == 1


def test_relay_publishes_outbox_message():
    """Relay publishes unpublished outbox messages to RabbitMQ."""
    assert callable(relay.publish_outbox_messages)

    payment_reference = "pay_relay_test"
    outbox_row_doc = {
        "event_type": "OrderRequested",
        "payload": {
            "event_type": "OrderRequested",
            "payment_reference": payment_reference,
            "checkout_id": "chk_123",
            "email": "guest@example.com",
            "ship_to": _ship_to(),
            "lines": [{"product_id": "prod1", "quantity": 1, "price_minor_units": 1000}],
            "total_minor_units": 1000,
            "timestamp": datetime.utcnow().isoformat(),
        },
        "payment_reference": payment_reference,
        "published": False,
        "published_at": None,
        "created_at": datetime.utcnow(),
    }
    outbox_collection.insert_one(outbox_row_doc)

    outbox_row = outbox_collection.find_one({"payment_reference": payment_reference})
    assert outbox_row is not None
    assert outbox_row["published"] is False


def test_completion_checker_inserts_refund_request():
    """Completion checker inserts RefundRequested for expired checkouts."""
    assert callable(completion_checker.check_expired_checkouts)

    checkout_id = "chk_expired"
    payment_reference = "pay_expired"
    checkout = {
        "checkout_id": checkout_id,
        "payment_reference": payment_reference,
        "cart_id": "cart_expired",
        "email": "guest@example.com",
        "ship_to": _ship_to(),
        "lines": [
            {"product_id": "prod1", "quantity": 1, "price_snapshot_minor_units": 1000}
        ],
        "total_minor_units": 1000,
        "status": "accepted",
        "order_id": None,
        "created_at": datetime.utcnow() - timedelta(minutes=6),
        "updated_at": datetime.utcnow() - timedelta(minutes=6),
    }
    checkouts_collection.insert_one(checkout)

    inserted_checkout = checkouts_collection.find_one({"checkout_id": checkout_id})
    assert inserted_checkout is not None
    assert inserted_checkout["status"] == "accepted"


def test_idempotency_duplicate_payment_reference():
    """AC4: Duplicate cart_id returns existing checkout."""
    cart_id = "cart-dup"
    _seed_cart(cart_id, quantity=1, price=1000)

    body = {
        "cart_id": cart_id,
        "email": "guest@example.com",
        "ship_to": _ship_to(),
    }

    response1 = client.post("/checkout", json=body)
    assert response1.status_code == 200
    checkout_id_1 = response1.json()["checkout_id"]

    response2 = client.post("/checkout", json=body)
    assert response2.status_code == 200
    checkout_id_2 = response2.json()["checkout_id"]

    assert checkout_id_1 == checkout_id_2

    all_checkouts = [
        c
        for c in checkouts_data.values()
        if c.get("cart_id") == cart_id
        and c.get("status") in ["accepted", "order_created", "cart_cleared"]
    ]
    assert len(all_checkouts) == 1


def test_database_isolation_checkout_side():
    """AC8: Checkout reads and writes only checkout_db."""
    cart_id = "cart-iso-side"
    _seed_cart(cart_id, quantity=1, price=1000)

    response = client.post(
        "/checkout",
        json={
            "cart_id": cart_id,
            "email": "guest@example.com",
            "ship_to": _ship_to(),
        },
    )
    assert response.status_code == 200

    accessed_databases = [
        call.args[0] for call in mongo_client.__getitem__.call_args_list
    ]
    assert accessed_databases == ["checkout_db"]

    accessed_collections = [
        call.args[0] for call in checkout_db.__getitem__.call_args_list
    ]
    assert set(accessed_collections) == {"checkouts", "outbox"}

    for path in SERVICE_ROOT.glob("*.py"):
        source = path.read_text()
        for name in OTHER_DATABASES:
            assert name not in source, f"{name} found in {path}"


def test_relay_publishes_order_and_refund_then_marks_published(monkeypatch):
    order_id = ObjectId()
    refund_id = ObjectId()
    rows = [
        {
            "_id": order_id,
            "event_type": "OrderRequested",
            "payload": {"event_type": "OrderRequested", "payment_reference": "pay_order"},
            "payment_reference": "pay_order",
            "published": False,
        },
        {
            "_id": refund_id,
            "event_type": "RefundRequested",
            "payload": {"event_type": "RefundRequested", "payment_reference": "pay_refund"},
            "payment_reference": "pay_refund",
            "published": False,
        },
    ]
    cursor = MagicMock()
    cursor.limit.return_value = rows
    outbox_collection.find.return_value = cursor
    updates = []

    def update_one(query, update):
        updates.append((query, update))

    outbox_collection.update_one.side_effect = update_one

    channel = MagicMock()
    connection = MagicMock()
    connection.channel.return_value = channel
    monkeypatch.setattr(relay, "get_rabbitmq_connection", lambda: connection)

    def stop(_seconds):
        raise RuntimeError("stop-poll")

    monkeypatch.setattr(relay.time, "sleep", stop)

    try:
        relay.publish_outbox_messages()
    except RuntimeError as exc:
        assert str(exc) == "stop-poll"

    assert outbox_collection.find.call_args.args[0] == {"published": False}
    cursor.limit.assert_called_with(100)
    channel.exchange_declare.assert_called_once_with(
        exchange="ecom.events", exchange_type="topic", durable=True
    )
    channel.confirm_delivery.assert_called_once()
    published = channel.basic_publish.call_args_list
    assert [call.kwargs["routing_key"] for call in published] == [
        "order.requested",
        "payment.refund.requested",
    ]
    assert json.loads(published[0].kwargs["body"])["payment_reference"] == "pay_order"
    assert published[0].kwargs["properties"].delivery_mode == 2
    assert published[0].kwargs["properties"].content_type == "application/json"
    assert [query["_id"] for query, _update in updates] == [order_id, refund_id]
    assert updates[0][1]["$set"]["published"] is True
    assert updates[0][1]["$set"]["published_at"] is not None


def test_relay_leaves_row_unpublished_when_broker_rejects(monkeypatch):
    row_id = ObjectId()
    cursor = MagicMock()
    cursor.limit.return_value = [
        {
            "_id": row_id,
            "event_type": "OrderRequested",
            "payload": {"event_type": "OrderRequested"},
            "payment_reference": "pay_nack",
            "published": False,
        }
    ]
    outbox_collection.find.return_value = cursor
    updates = []

    def update_one(query, update):
        updates.append((query, update))

    outbox_collection.update_one.side_effect = update_one

    channel = MagicMock()
    channel.basic_publish.side_effect = RuntimeError("nack")
    connection = MagicMock()
    connection.channel.return_value = channel
    monkeypatch.setattr(relay, "get_rabbitmq_connection", lambda: connection)

    def stop(_seconds):
        raise RuntimeError("stop-poll")

    monkeypatch.setattr(relay.time, "sleep", stop)

    try:
        relay.publish_outbox_messages()
    except RuntimeError as exc:
        assert str(exc) == "stop-poll"

    assert updates == []


def test_completion_checker_requests_refund_for_expired_checkout(monkeypatch):
    checkout_oid = ObjectId()
    stored = {}

    def find(query):
        stored["query"] = query
        return [
            {
                "_id": checkout_oid,
                "checkout_id": "chk_window",
                "payment_reference": "pay_window",
            }
        ]

    def insert_one(row):
        stored["outbox"] = row

    def update_one(query, update):
        stored["update_query"] = query
        stored["update"] = update

    checkouts_collection.find.side_effect = find
    outbox_collection.insert_one.side_effect = insert_one
    checkouts_collection.update_one.side_effect = update_one

    def stop(_seconds):
        raise RuntimeError("stop-poll")

    monkeypatch.setattr(completion_checker.time, "sleep", stop)

    try:
        completion_checker.check_expired_checkouts()
    except RuntimeError as exc:
        assert str(exc) == "stop-poll"

    assert stored["query"]["status"] == "accepted"
    assert "$lt" in stored["query"]["created_at"]
    assert stored["outbox"]["event_type"] == "RefundRequested"
    assert stored["outbox"]["published"] is False
    assert stored["outbox"]["payload"]["checkout_id"] == "chk_window"
    assert stored["outbox"]["payload"]["payment_reference"] == "pay_window"
    assert stored["outbox"]["payment_reference"] == "pay_window"
    assert stored["update_query"] == {"_id": checkout_oid}
    assert stored["update"]["$set"]["status"] == "refund_pending"


def test_checkout_status_refund_pending(monkeypatch):
    """AC6: Completion checker updates checkout status to refund_pending."""
    checkout_oid = ObjectId()
    now = datetime.utcnow() - timedelta(minutes=6)
    checkout = {
        "_id": checkout_oid,
        "checkout_id": "chk_pending",
        "payment_reference": "pay_pending",
        "cart_id": "cart_pending",
        "email": "guest@example.com",
        "ship_to": _ship_to(),
        "lines": [
            {"product_id": "prod_001", "quantity": 1, "price_snapshot_minor_units": 1000}
        ],
        "total_minor_units": 1000,
        "status": "accepted",
        "order_id": None,
        "created_at": now,
        "updated_at": now,
    }
    checkouts_data["chk_pending"] = checkout

    def find(query):
        assert query["status"] == "accepted"
        assert "$lt" in query["created_at"]
        return [checkout]

    def insert_one(row):
        outbox_data[str(row["_id"])] = row

    def update_one(query, update):
        assert query == {"_id": checkout_oid}
        checkout.update(update["$set"])

    checkouts_collection.find.side_effect = find
    outbox_collection.insert_one.side_effect = insert_one
    checkouts_collection.update_one.side_effect = update_one

    def stop(_seconds):
        raise RuntimeError("stop-poll")

    monkeypatch.setattr(completion_checker.time, "sleep", stop)

    try:
        completion_checker.check_expired_checkouts()
    except RuntimeError as exc:
        assert str(exc) == "stop-poll"

    updated_checkout = checkouts_collection.find_one({"checkout_id": "chk_pending"})
    assert updated_checkout["status"] == "refund_pending"

    refund_row = outbox_collection.find_one(
        {"payment_reference": "pay_pending", "event_type": "RefundRequested"}
    )
    assert refund_row is not None
    assert refund_row["published"] is False
    assert refund_row["payload"]["payment_reference"] == "pay_pending"
    assert refund_row["payload"]["checkout_id"] == "chk_pending"


def _status_checkout(checkout_id, payment_reference, cart_id, **overrides):
    checkout = {
        "_id": ObjectId(),
        "checkout_id": checkout_id,
        "payment_reference": payment_reference,
        "cart_id": cart_id,
        "email": "guest@example.com",
        "ship_to": {
            "street": "123 Main St",
            "city": "Springfield",
            "postal_code": "12345",
            "country": "US",
        },
        "lines": [
            {"product_id": "prod_001", "quantity": 1, "price_snapshot_minor_units": 1000}
        ],
        "total_minor_units": 1000,
        "status": "accepted",
        "order_id": None,
        "created_at": datetime(2025, 1, 10, 14, 30, 0),
        "updated_at": datetime(2025, 1, 10, 14, 30, 0),
    }
    checkout.update(overrides)
    return checkout


def test_checkout_status_accepted():
    checkouts_collection.delete_many({})
    checkouts_collection.insert_one(
        _status_checkout(
            "chk_test_1",
            "pay_test_1",
            "cart_test_1",
            lines=[
                {
                    "product_id": "prod_001",
                    "quantity": 2,
                    "price_snapshot_minor_units": 1500,
                }
            ],
            total_minor_units=3000,
            status="accepted",
            order_id=None,
        )
    )

    response = client.get("/checkout/chk_test_1")

    assert response.status_code == 200
    body = response.json()
    assert body["checkout_id"] == "chk_test_1"
    assert body["status"] == "accepted"
    assert body["order_id"] is None
    assert body["created_at"] == "2025-01-10T14:30:00"


def test_checkout_status_order_created():
    checkouts_collection.delete_many({})
    checkouts_collection.insert_one(
        _status_checkout(
            "chk_test_2",
            "pay_test_2",
            "cart_test_2",
            status="order_created",
            order_id="ord_test_1",
            created_at=datetime(2025, 1, 10, 14, 30, 5),
            updated_at=datetime(2025, 1, 10, 14, 30, 5),
        )
    )

    response = client.get("/checkout/chk_test_2")

    assert response.status_code == 200
    body = response.json()
    assert body["checkout_id"] == "chk_test_2"
    assert body["status"] == "order_created"
    assert body["order_id"] == "ord_test_1"
    assert body["created_at"] == "2025-01-10T14:30:05"


def test_checkout_status_cart_cleared():
    checkouts_collection.delete_many({})
    checkouts_collection.insert_one(
        _status_checkout(
            "chk_test_3",
            "pay_test_3",
            "cart_test_3",
            status="cart_cleared",
            order_id="ord_test_2",
            created_at=datetime(2025, 1, 10, 14, 30, 10),
            updated_at=datetime(2025, 1, 10, 14, 30, 15),
        )
    )

    response = client.get("/checkout/chk_test_3")

    assert response.status_code == 200
    body = response.json()
    assert body["checkout_id"] == "chk_test_3"
    assert body["status"] == "cart_cleared"
    assert body["order_id"] == "ord_test_2"
    assert body["created_at"] == "2025-01-10T14:30:10"


def test_checkout_status_refunded():
    checkouts_collection.delete_many({})
    checkouts_collection.insert_one(
        _status_checkout(
            "chk_test_4",
            "pay_test_4",
            "cart_test_4",
            status="refunded",
            order_id=None,
            created_at=datetime(2025, 1, 10, 14, 35, 0),
            updated_at=datetime(2025, 1, 10, 14, 40, 0),
        )
    )

    response = client.get("/checkout/chk_test_4")

    assert response.status_code == 200
    body = response.json()
    assert body["checkout_id"] == "chk_test_4"
    assert body["status"] == "refunded"
    assert body["order_id"] is None
    assert body["created_at"] == "2025-01-10T14:35:00"


def test_checkout_not_found():
    checkouts_collection.delete_many({})

    response = client.get("/checkout/chk_missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "Checkout not found"}


def test_checkout_status_missing_created_at():
    checkouts_collection.delete_many({})
    checkout = _status_checkout("chk_old", "pay_old", "cart_old")
    del checkout["created_at"]
    checkouts_collection.insert_one(checkout)

    response = client.get("/checkout/chk_old")

    assert response.status_code == 200
    body = response.json()
    assert body["checkout_id"] == "chk_old"
    assert body["status"] == "accepted"
    assert body["created_at"] == "1970-01-01T00:00:00Z"


def test_checkout_status_missing_status_field():
    checkouts_collection.delete_many({})
    checkout = _status_checkout(
        "chk_old_status", "pay_old_status", "cart_old_status"
    )
    del checkout["status"]
    checkouts_collection.insert_one(checkout)

    response = client.get("/checkout/chk_old_status")

    assert response.status_code == 200
    body = response.json()
    assert body["checkout_id"] == "chk_old_status"
    assert body["status"] == "accepted"
    assert body["order_id"] is None


def test_checkout_status_requires_order_id_when_order_exists():
    try:
        CheckoutStatusResponse(
            checkout_id="chk_missing_order",
            status="order_created",
            created_at="2025-01-10T14:30:00",
        )
    except ValidationError as exc:
        assert "order_id must be present" in str(exc)
    else:
        raise AssertionError("expected validation error")

    try:
        CheckoutStatusResponse(
            checkout_id="chk_missing_order",
            status="cart_cleared",
            order_id=None,
            created_at="2025-01-10T14:30:00",
        )
    except ValidationError as exc:
        assert "order_id must be present" in str(exc)
    else:
        raise AssertionError("expected validation error")


def test_checkout_status_database_query_failed():
    checkouts_collection.find_one.side_effect = PyMongoError("down")

    response = client.get("/checkout/chk_db_down")

    assert response.status_code == 500
    assert response.json() == {"detail": "Database query failed"}
