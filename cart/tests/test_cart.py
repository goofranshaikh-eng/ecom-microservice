import uuid
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

import routes
from db import carts_collection
from db import client as mongo_client
from db import db as cart_db
from main import app

client = TestClient(app)
SERVICE_ROOT = Path(__file__).resolve().parents[1]
OTHER_DATABASES = (
    "identity_db",
    "catalog_db",
    "checkout_db",
    "payment_db",
    "order_db",
)

carts: dict = {}
catalog_products: dict = {}
catalog_calls: list = []


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
        catalog_calls.append(url)
        product_id = url.rstrip("/").rsplit("/", 1)[-1]
        product = catalog_products.get(product_id)
        if product is None:
            return _FakeResponse(404, {})
        return _FakeResponse(200, product)


def setup_function():
    carts.clear()
    catalog_products.clear()
    catalog_calls.clear()
    routes.httpx.AsyncClient = _FakeAsyncClient

    def insert_one(doc):
        carts[doc["_id"]] = {
            "_id": doc["_id"],
            "lines": [dict(line) for line in doc["lines"]],
            "total_minor_units": doc["total_minor_units"],
        }
        return MagicMock(inserted_id=doc["_id"])

    def find_one(query):
        doc = carts.get(query["_id"])
        if doc is None:
            return None
        return {
            "_id": doc["_id"],
            "lines": [dict(line) for line in doc["lines"]],
            "total_minor_units": doc["total_minor_units"],
        }

    def update_one(query, update):
        result = MagicMock()
        doc = carts.get(query["_id"])
        if doc is None:
            result.matched_count = 0
            return result
        doc["lines"] = [dict(line) for line in update["$set"]["lines"]]
        doc["total_minor_units"] = update["$set"]["total_minor_units"]
        result.matched_count = 1
        return result

    def delete_one(query):
        result = MagicMock()
        if carts.pop(query["_id"], None) is None:
            result.deleted_count = 0
        else:
            result.deleted_count = 1
        return result

    carts_collection.insert_one.side_effect = insert_one
    carts_collection.find_one.side_effect = find_one
    carts_collection.update_one.side_effect = update_one
    carts_collection.delete_one.side_effect = delete_one


def _create_cart() -> str:
    response = client.post("/cart")
    assert response.status_code == 201
    return response.json()["cart_id"]


def test_create_cart():
    response = client.post("/cart")

    assert response.status_code == 201
    cart_id = response.json()["cart_id"]
    uuid.UUID(cart_id)
    assert carts[cart_id] == {
        "_id": cart_id,
        "lines": [],
        "total_minor_units": 0,
    }


def test_add_sellable_item_to_cart():
    catalog_products["prod1"] = {
        "name": "Widget",
        "price_minor_units": 1000,
        "sellable": True,
    }
    cart_id = _create_cart()

    response = client.post(
        f"/cart/{cart_id}/items",
        json={"product_id": "prod1", "quantity": 2},
    )

    assert response.status_code == 200
    assert response.json() == {
        "cart_id": cart_id,
        "lines": [
            {
                "product_id": "prod1",
                "quantity": 2,
                "price_snapshot_minor_units": 1000,
            }
        ],
        "total_minor_units": 2000,
    }
    assert catalog_calls == ["http://localhost:8002/products/prod1"]


def test_add_unsellable_item_refused():
    catalog_products["prod2"] = {
        "name": "Gadget",
        "price_minor_units": 2000,
        "sellable": False,
    }
    cart_id = _create_cart()

    response = client.post(
        f"/cart/{cart_id}/items",
        json={"product_id": "prod2", "quantity": 1},
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "Product cannot be added",
        "reason": "Product is not sellable",
    }
    fetched = client.get(f"/cart/{cart_id}")
    assert fetched.status_code == 200
    assert fetched.json() == {
        "cart_id": cart_id,
        "lines": [],
        "total_minor_units": 0,
    }


def test_update_item_quantity():
    catalog_products["prod1"] = {
        "name": "Widget",
        "price_minor_units": 1000,
        "sellable": True,
    }
    cart_id = _create_cart()
    added = client.post(
        f"/cart/{cart_id}/items",
        json={"product_id": "prod1", "quantity": 2},
    )
    assert added.status_code == 200
    calls_after_add = len(catalog_calls)

    response = client.patch(
        f"/cart/{cart_id}/items/prod1",
        json={"quantity": 3},
    )

    assert response.status_code == 200
    assert response.json() == {
        "cart_id": cart_id,
        "lines": [
            {
                "product_id": "prod1",
                "quantity": 3,
                "price_snapshot_minor_units": 1000,
            }
        ],
        "total_minor_units": 3000,
    }
    assert len(catalog_calls) == calls_after_add


def test_remove_line_with_zero_quantity():
    catalog_products["prod1"] = {
        "name": "Widget",
        "price_minor_units": 1000,
        "sellable": True,
    }
    cart_id = _create_cart()
    added = client.post(
        f"/cart/{cart_id}/items",
        json={"product_id": "prod1", "quantity": 2},
    )
    assert added.status_code == 200

    response = client.patch(
        f"/cart/{cart_id}/items/prod1",
        json={"quantity": 0},
    )

    assert response.status_code == 200
    assert response.json() == {
        "cart_id": cart_id,
        "lines": [],
        "total_minor_units": 0,
    }


def test_get_cart_returns_current_state():
    catalog_products["prod1"] = {
        "name": "Widget",
        "price_minor_units": 1000,
        "sellable": True,
    }
    catalog_products["prod3"] = {
        "name": "Gizmo",
        "price_minor_units": 500,
        "sellable": True,
    }
    cart_id = _create_cart()
    assert (
        client.post(
            f"/cart/{cart_id}/items",
            json={"product_id": "prod1", "quantity": 2},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/cart/{cart_id}/items",
            json={"product_id": "prod3", "quantity": 1},
        ).status_code
        == 200
    )

    response = client.get(f"/cart/{cart_id}")

    assert response.status_code == 200
    assert response.json() == {
        "cart_id": cart_id,
        "lines": [
            {
                "product_id": "prod1",
                "quantity": 2,
                "price_snapshot_minor_units": 1000,
            },
            {
                "product_id": "prod3",
                "quantity": 1,
                "price_snapshot_minor_units": 500,
            },
        ],
        "total_minor_units": 2500,
    }


def test_delete_cart():
    cart_id = _create_cart()

    response = client.delete(f"/cart/{cart_id}")

    assert response.status_code == 204
    assert response.content == b""
    fetched = client.get(f"/cart/{cart_id}")
    assert fetched.status_code == 404
    assert fetched.json() == {"detail": "Cart not found"}


def test_swagger_ui_lists_cart_endpoints():
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "Cart Service" in docs.text

    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    schema = spec.json()
    assert schema["info"]["title"] == "Cart Service"
    paths = schema["paths"]
    assert "post" in paths["/cart"]
    assert "get" in paths["/cart/{cart_id}"]
    assert "post" in paths["/cart/{cart_id}/items"]
    assert "patch" in paths["/cart/{cart_id}/items/{product_id}"]
    assert "delete" in paths["/cart/{cart_id}"]

    add_item = paths["/cart/{cart_id}/items"]["post"]
    update_item = paths["/cart/{cart_id}/items/{product_id}"]["patch"]
    assert "CartResponse" in str(add_item["responses"]["200"])
    assert "AddItemRequest" in str(add_item["requestBody"])
    assert "ErrorResponse" in str(add_item["responses"]["400"])
    assert "CartResponse" in str(update_item["responses"]["200"])
    assert "UpdateItemRequest" in str(update_item["requestBody"])

    schemas = schema["components"]["schemas"]
    assert "CartLine" in schemas
    assert "CartResponse" in schemas
    assert "AddItemRequest" in schemas
    assert "UpdateItemRequest" in schemas
    assert "ErrorResponse" in schemas
    assert "CreateCartResponse" in schemas


def test_database_isolation():
    catalog_products["prod1"] = {
        "name": "Widget",
        "price_minor_units": 1000,
        "sellable": True,
    }
    cart_id = _create_cart()
    added = client.post(
        f"/cart/{cart_id}/items",
        json={"product_id": "prod1", "quantity": 1},
    )
    assert added.status_code == 200
    assert carts[cart_id]["total_minor_units"] == 1000

    accessed_databases = [
        call.args[0] for call in mongo_client.__getitem__.call_args_list
    ]
    accessed_collections = [call.args[0] for call in cart_db.__getitem__.call_args_list]
    assert accessed_databases == ["cart_db"]
    assert accessed_collections == ["carts"]
    assert carts_collection.insert_one.called
    assert carts_collection.update_one.called
    assert catalog_calls == ["http://localhost:8002/products/prod1"]

    for path in SERVICE_ROOT.glob("*.py"):
        source = path.read_text()
        for name in OTHER_DATABASES:
            assert name not in source
