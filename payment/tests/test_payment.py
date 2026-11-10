import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

from bson import ObjectId
from fastapi.testclient import TestClient

from db import charges_collection
from db import client as mongo_client
from db import db as payment_db
from main import app

client = TestClient(app)
SERVICE_ROOT = Path(__file__).resolve().parents[1]
OTHER_DATABASES = (
    "identity_db",
    "catalog_db",
    "cart_db",
    "checkout_db",
    "order_db",
)

charges: list = []


def setup_function():
    charges.clear()

    def insert_one(doc):
        charges.append(dict(doc))
        return MagicMock(inserted_id=doc["_id"])

    charges_collection.insert_one.reset_mock(side_effect=False, return_value=False)
    charges_collection.insert_one.side_effect = insert_one


def _assert_recent(timestamp: datetime) -> None:
    assert isinstance(timestamp, datetime)
    assert abs((datetime.utcnow() - timestamp).total_seconds()) < 1


def test_charge_accepted_low_amount():
    response = client.post("/charge", json={"amount_minor_units": 5000})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"status", "reference"}
    assert body["status"] == "accepted"
    uuid.UUID(body["reference"])

    assert len(charges) == 1
    doc = charges[0]
    assert isinstance(doc["_id"], ObjectId)
    assert doc["amount_minor_units"] == 5000
    assert doc["status"] == "accepted"
    assert doc["reference"] == body["reference"]
    assert "reason" not in doc
    _assert_recent(doc["timestamp"])


def test_charge_refused_high_amount():
    response = client.post("/charge", json={"amount_minor_units": 150000})

    assert response.status_code == 200
    assert response.json() == {"status": "refused", "reason": "Amount exceeds limit"}

    assert len(charges) == 1
    doc = charges[0]
    assert doc["amount_minor_units"] == 150000
    assert doc["status"] == "refused"
    assert doc["reason"] == "Amount exceeds limit"
    assert "reference" not in doc
    _assert_recent(doc["timestamp"])


def test_charge_refused_card_ending_0000():
    response = client.post(
        "/charge",
        json={"amount_minor_units": 5000, "card": "1234567890000000"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "refused", "reason": "Card declined"}

    assert len(charges) == 1
    doc = charges[0]
    assert doc["amount_minor_units"] == 5000
    assert doc["status"] == "refused"
    assert doc["reason"] == "Card declined"
    assert "reference" not in doc
    assert "card" not in doc
    _assert_recent(doc["timestamp"])


def test_charge_records_stored():
    accepted = client.post("/charge", json={"amount_minor_units": 1000})
    refused = client.post("/charge", json={"amount_minor_units": 200000})

    assert accepted.status_code == 200
    assert refused.status_code == 200
    assert len(charges) == 2

    first, second = charges
    assert first["status"] == "accepted"
    assert first["reference"] == accepted.json()["reference"]
    uuid.UUID(first["reference"])
    assert "reason" not in first

    assert second["status"] == "refused"
    assert second["reason"] == "Amount exceeds limit"
    assert "reference" not in second


def test_swagger_ui_lists_charge_endpoint():
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "Payment Service" in docs.text

    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    schema = spec.json()
    assert schema["info"]["title"] == "Payment Service"
    charge_post = schema["paths"]["/charge"]["post"]
    assert "ChargeRequest" in str(charge_post["requestBody"])
    assert "ChargeResponse" in str(charge_post["responses"]["200"])

    schemas = schema["components"]["schemas"]
    request_schema = schemas["ChargeRequest"]
    assert request_schema["properties"]["amount_minor_units"]["type"] == "integer"
    assert "amount_minor_units" in request_schema["required"]
    card_schema = request_schema["properties"]["card"]
    assert "string" in str(card_schema)
    assert "card" not in request_schema.get("required", [])

    response_schema = schemas["ChargeResponse"]
    assert "status" in response_schema["properties"]
    assert "reference" in response_schema["properties"]
    assert "reason" in response_schema["properties"]
    assert "accepted" in str(response_schema)
    assert "refused" in str(response_schema)


def test_database_isolation():
    accepted = client.post("/charge", json={"amount_minor_units": 2500})
    refused = client.post(
        "/charge",
        json={"amount_minor_units": 2500, "card": "4111111111110000"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"
    assert refused.status_code == 200
    assert refused.json()["status"] == "refused"
    assert len(charges) == 2
    assert charges_collection.insert_one.called

    accessed_databases = [
        call.args[0] for call in mongo_client.__getitem__.call_args_list
    ]
    accessed_collections = [
        call.args[0] for call in payment_db.__getitem__.call_args_list
    ]
    assert accessed_databases == ["payment_db"]
    assert accessed_collections == ["charges"]

    for path in SERVICE_ROOT.glob("*.py"):
        source = path.read_text()
        for name in OTHER_DATABASES:
            assert name not in source
        assert "import requests" not in source
        assert "import httpx" not in source
        assert "import aiohttp" not in source
