from bson import ObjectId
from fastapi.testclient import TestClient

from db import shoppers_collection
from main import app

client = TestClient(app)


def setup_function():
    shoppers_collection.find_one.reset_mock(side_effect=True, return_value=True)
    shoppers_collection.find_one.return_value = None


def test_sign_in_success():
    oid = ObjectId()
    shoppers_collection.find_one.return_value = {
        "_id": oid,
        "email": "test@example.com",
        "password": "password123",
    }

    response = client.post(
        "/sign-in",
        json={"email": "test@example.com", "password": "password123"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["shopper_id"] == str(oid)
    ObjectId(body["shopper_id"])
    assert body["token"] == "stub_token_v1"
    shoppers_collection.find_one.assert_called_once_with({"email": "test@example.com"})


def test_sign_in_invalid_credentials():
    shoppers_collection.find_one.return_value = {
        "_id": ObjectId(),
        "email": "test@example.com",
        "password": "password123",
    }

    response = client.post(
        "/sign-in",
        json={"email": "test@example.com", "password": "wrongpassword"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid credentials"}


def test_shopper_lookup_success():
    oid = ObjectId()
    shoppers_collection.find_one.return_value = {
        "_id": oid,
        "email": "test@example.com",
        "password": "password123",
    }

    response = client.get(f"/shoppers/{oid}")

    assert response.status_code == 200
    assert response.json() == {"shopper_id": str(oid), "email": "test@example.com"}
    shoppers_collection.find_one.assert_called_once_with({"_id": oid})


def test_shopper_lookup_invalid_id():
    response = client.get("/shoppers/invalid-id-format")

    assert response.status_code == 400
    assert "ObjectId" in response.json()["detail"]


def test_swagger_ui_lists_identity_endpoints():
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "Identity Service" in docs.text

    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    schema = spec.json()
    assert schema["info"]["title"] == "Identity Service"
    assert "/sign-in" in schema["paths"]
    assert "/shoppers/{shopper_id}" in schema["paths"]
    assert "SignInRequest" in schema["components"]["schemas"]
    assert "SignInResponse" in schema["components"]["schemas"]
    assert "ShopperResponse" in schema["components"]["schemas"]
