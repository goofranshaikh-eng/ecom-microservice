from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, HTTPException

from db import shoppers_collection
from models import SignInRequest, SignInResponse, ShopperResponse

# All identity endpoints are public in v1. There is no role-based access control.
router = APIRouter()


@router.post(
    "/sign-in",
    response_model=SignInResponse,
    summary="Sign in a shopper",
    description=(
        "Public endpoint (no authorization in v1). "
        "[STUB_AUTH] compares the stored password in plaintext. "
        "[STUB_TOKEN] returns the fixed token stub_token_v1."
    ),
    responses={
        401: {
            "description": "Invalid credentials",
            "content": {
                "application/json": {"example": {"detail": "Invalid credentials"}}
            },
        }
    },
)
def sign_in(body: SignInRequest) -> SignInResponse:
    doc = shoppers_collection.find_one({"email": str(body.email)})
    if doc is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    # [STUB_AUTH] plaintext comparison; no bcrypt in v1.
    if doc["password"] != body.password:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    # [STUB_TOKEN] hardcoded stub; no JWT, expiry, or validation.
    return SignInResponse(shopper_id=str(doc["_id"]), token="stub_token_v1")


@router.get(
    "/shoppers/{shopper_id}",
    response_model=ShopperResponse,
    summary="Look up a shopper",
    description="Public endpoint (no authorization in v1). Returns the shopper id and email.",
    responses={
        400: {
            "description": "Invalid ObjectId",
            "content": {
                "application/json": {"example": {"detail": "Invalid ObjectId"}}
            },
        },
        404: {
            "description": "Shopper not found",
            "content": {
                "application/json": {"example": {"detail": "Shopper not found"}}
            },
        },
    },
)
def get_shopper(shopper_id: str) -> ShopperResponse:
    try:
        oid = ObjectId(shopper_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid ObjectId") from None
    doc = shoppers_collection.find_one({"_id": oid})
    if doc is None:
        raise HTTPException(status_code=404, detail="Shopper not found")
    return ShopperResponse(shopper_id=str(doc["_id"]), email=doc["email"])
