import re
from pathlib import Path

from bson import ObjectId
from fastapi.testclient import TestClient

from db import client as mongo_client
from db import db as catalog_db
from db import products_collection
from main import app

client = TestClient(app)
SERVICE_ROOT = Path(__file__).resolve().parents[1]
OTHER_DATABASES = (
    "identity_db",
    "cart_db",
    "checkout_db",
    "payment_db",
    "order_db",
)


def setup_function():
    products_collection.find.reset_mock(side_effect=True, return_value=True)
    products_collection.find_one.reset_mock(side_effect=True, return_value=True)
    products_collection.find.return_value = []
    products_collection.find_one.return_value = None


def test_search_returns_matching_products():
    widget_id = ObjectId()
    gadget_id = ObjectId()
    seeded = [
        {
            "_id": widget_id,
            "name": "Widget",
            "price_minor_units": 1000,
            "sellable": True,
        },
        {
            "_id": gadget_id,
            "name": "Gadget",
            "price_minor_units": 2000,
            "sellable": False,
        },
    ]

    def find(query):
        pattern = re.compile(query["name"]["$regex"], re.IGNORECASE)
        return [doc for doc in seeded if pattern.search(doc["name"])]

    products_collection.find.side_effect = find

    response = client.get("/products/search", params={"q": "widget"})

    assert response.status_code == 200
    assert response.json() == {
        "products": [
            {
                "id": str(widget_id),
                "name": "Widget",
                "price_minor_units": 1000,
                "sellable": True,
            }
        ]
    }
    products_collection.find.assert_called_once_with(
        {"name": {"$regex": "widget", "$options": "i"}}
    )


def test_fetch_sellable_product():
    product_id = ObjectId()
    products_collection.find_one.return_value = {
        "_id": product_id,
        "name": "Widget",
        "price_minor_units": 1000,
        "sellable": True,
    }

    response = client.get(f"/products/{product_id}")

    assert response.status_code == 200
    assert response.json() == {
        "id": str(product_id),
        "name": "Widget",
        "price_minor_units": 1000,
        "sellable": True,
    }
    products_collection.find_one.assert_called_once_with({"_id": product_id})


def test_fetch_unsellable_product():
    product_id = ObjectId()
    products_collection.find_one.return_value = {
        "_id": product_id,
        "name": "Gadget",
        "price_minor_units": 2000,
        "sellable": False,
    }

    response = client.get(f"/products/{product_id}")

    assert response.status_code == 200
    assert response.json() == {
        "id": str(product_id),
        "name": "Gadget",
        "price_minor_units": 2000,
        "sellable": False,
    }


def test_product_not_found_returns_404():
    missing_id = "507f1f77bcf86cd799439011"

    response = client.get(f"/products/{missing_id}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Product not found"}
    products_collection.find_one.assert_called_once_with(
        {"_id": ObjectId(missing_id)}
    )


def test_swagger_ui_lists_catalog_endpoints():
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "Catalog Service" in docs.text

    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    schema = spec.json()
    assert schema["info"]["title"] == "Catalog Service"
    assert "/products/search" in schema["paths"]
    assert "/products/{product_id}" in schema["paths"]

    search = schema["paths"]["/products/search"]["get"]
    fetch = schema["paths"]["/products/{product_id}"]["get"]
    assert "ProductSearchResponse" in str(search["responses"]["200"])
    assert "Product" in str(fetch["responses"]["200"])
    assert "Product" in schema["components"]["schemas"]
    assert "ProductSearchResponse" in schema["components"]["schemas"]


def test_database_isolation():
    product_id = ObjectId()
    products_collection.insert_one(
        {
            "_id": product_id,
            "name": "Widget",
            "price_minor_units": 1000,
            "sellable": True,
        }
    )
    products_collection.find_one.return_value = {
        "_id": product_id,
        "name": "Widget",
        "price_minor_units": 1000,
        "sellable": True,
    }

    response = client.get(f"/products/{product_id}")
    assert response.status_code == 200

    accessed_databases = [call.args[0] for call in mongo_client.__getitem__.call_args_list]
    accessed_collections = [call.args[0] for call in catalog_db.__getitem__.call_args_list]
    assert accessed_databases == ["catalog_db"]
    assert accessed_collections == ["products"]
    assert products_collection.insert_one.called

    for path in SERVICE_ROOT.glob("*.py"):
        source = path.read_text()
        for name in OTHER_DATABASES:
            assert name not in source
        assert "import requests" not in source
        assert "import httpx" not in source
        assert "import aiohttp" not in source
