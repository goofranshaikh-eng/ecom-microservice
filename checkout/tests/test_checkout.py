import copy
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

import routes
from db import client as mongo_client
from db import db as checkout_db
from main import app

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
            amount = json["amount_minor_units"]
            if amount >= 100000:
                return _FakeResponse(
                    200, {"status": "refused", "reason": "Amount exceeds limit"}
                )
            return _FakeResponse(
                200, {"status": "accepted", "reference": "pay_ref_1"}
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
    order_status_code = 201
    delete_status_code = 204
    payment_status_code = 200
    cart_status_code = None
    routes.httpx.AsyncClient = _FakeAsyncClient


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
    assert body == {"order_id": "order-1", "status": "paid"}
    assert cart_id not in carts
    assert calls[1] == (
        "POST",
        "http://localhost:8005/charge",
        {"amount_minor_units": 2000},
    )
    order = orders["order-1"]
    assert order == {
        "email": "guest@example.com",
        "ship_to": ship_to,
        "lines": [
            {
                "product_id": "prod1",
                "quantity": 2,
                "price_minor_units": 1000,
            }
        ],
        "total_minor_units": 2000,
        "payment_reference": "pay_ref_1",
    }
    assert calls[0][0] == "GET"
    assert calls[0][1] == f"http://localhost:8003/cart/{cart_id}"
    assert calls[2][1] == "http://localhost:8006/orders"
    assert calls[3] == ("DELETE", f"http://localhost:8003/cart/{cart_id}", None)


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
    assert response.json() == {"order_id": "order-1", "status": "paid"}
    assert cart_id not in carts
    assert calls[1][2] == {"amount_minor_units": 1000}
    assert orders["order-1"]["shopper_id"] == "shopper-1"
    assert "email" not in orders["order-1"]
    assert orders["order-1"]["ship_to"] == ship_to
    assert orders["order-1"]["lines"] == [
        {
            "product_id": "prod1",
            "quantity": 1,
            "price_minor_units": 1000,
        }
    ]
    assert orders["order-1"]["total_minor_units"] == 1000
    assert orders["order-1"]["payment_reference"] == "pay_ref_1"


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
    assert response.json() == {
        "status": "refused",
        "reason": "Amount exceeds limit",
    }
    assert carts[cart_id] == before
    assert orders == {}
    assert not any(call[0] == "DELETE" for call in calls)
    assert not any(call[1].endswith("/orders") for call in calls)
    assert calls[1][2] == {"amount_minor_units": 200000}


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


def test_cart_emptied_on_payment_success():
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
    assert response.json()["status"] == "paid"
    assert cart_id not in carts


def test_order_created_on_payment_success():
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
    order_id = response.json()["order_id"]
    order = orders[order_id]
    assert order["email"] == "guest@example.com"
    assert order["ship_to"] == ship_to
    assert order["lines"][0]["product_id"] == "prod1"
    assert order["lines"][0]["quantity"] == 2
    assert order["total_minor_units"] == 2000
    assert order["payment_reference"] == "pay_ref_1"


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
    assert "CheckoutResponse" in str(checkout_post["responses"]["200"])
    schemas = schema["components"]["schemas"]
    assert "CheckoutRequest" in schemas
    assert "CheckoutResponse" in schemas
    assert "ShipTo" in schemas
    assert "paid" in str(schemas["CheckoutResponse"])
    assert "refused" in str(schemas["CheckoutResponse"])


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


def test_downstream_unavailable():
    global cart_status_code, payment_status_code, order_status_code
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

    cart_status_code = None
    payment_status_code = 503
    payment_down = client.post("/checkout", json=body)
    assert payment_down.status_code == 500
    assert payment_down.json() == {"detail": "Payment service unavailable"}
    assert cart_id in carts

    payment_status_code = 200
    order_status_code = 500
    order_down = client.post("/checkout", json=body)
    assert order_down.status_code == 500
    assert order_down.json() == {"detail": "Order service unavailable"}
    assert cart_id in carts
    assert orders == {}


def test_cart_delete_failure_still_returns_paid(capsys):
    global delete_status_code
    cart_id = "cart-delete-fails"
    _seed_cart(cart_id, quantity=1)
    delete_status_code = 500

    response = client.post(
        "/checkout",
        json={
            "cart_id": cart_id,
            "email": "guest@example.com",
            "ship_to": _ship_to(),
        },
    )

    assert response.status_code == 200
    assert response.json() == {"order_id": "order-1", "status": "paid"}
    assert cart_id in carts
    assert "order-1" in orders
    captured = capsys.readouterr()
    assert f"Failed to delete cart {cart_id} after checkout" in captured.out


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
    assert calls[1][2] == {"amount_minor_units": 1500}
    assert orders["order-1"]["total_minor_units"] == 1500


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
    assert accessed_collections == []

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
