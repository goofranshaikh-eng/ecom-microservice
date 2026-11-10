import uuid

import httpx
from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from db import carts_collection
from models import (
    AddItemRequest,
    CartLine,
    CartResponse,
    CreateCartResponse,
    ErrorResponse,
    UpdateItemRequest,
)

# All cart endpoints are public in v1. [AUTH_DEFERRED]
# [LOGGING_DEFERRED] no structured logging in v1.
# [MAX_QUANTITY_DEFERRED] a line may hold any positive quantity.
# [ERROR_DETAIL_DEFERRED] 500 responses are not sanitized.
# [CATALOG_URL_HARDCODED] future: read CATALOG_SERVICE_URL from the environment.
CATALOG_BASE_URL = "http://localhost:8002"

router = APIRouter()


def _total_minor_units(lines: list) -> int:
    return sum(line["price_snapshot_minor_units"] * line["quantity"] for line in lines)


def _to_cart_response(cart_id: str, lines: list, total_minor_units: int) -> CartResponse:
    return CartResponse(
        cart_id=cart_id,
        lines=[CartLine(**line) for line in lines],
        total_minor_units=total_minor_units,
    )


def _error(error: str, reason: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content=ErrorResponse(error=error, reason=reason).model_dump(),
    )


@router.post(
    "/cart",
    status_code=201,
    response_model=CreateCartResponse,
    summary="Create a cart",
    description="Public endpoint (no authorization in v1). [AUTH_DEFERRED]",
    responses={
        500: {
            "description": "Database insertion failure",
            "content": {
                "application/json": {
                    "example": {"detail": "Database insertion failure"}
                }
            },
        }
    },
)
def create_cart() -> CreateCartResponse:
    cart_id = str(uuid.uuid4())
    doc = {"_id": cart_id, "lines": [], "total_minor_units": 0}
    try:
        carts_collection.insert_one(doc)
    except PyMongoError:
        raise HTTPException(
            status_code=500, detail="Database insertion failure"
        ) from None
    return CreateCartResponse(cart_id=cart_id)


@router.get(
    "/cart/{cart_id}",
    response_model=CartResponse,
    summary="Get a cart",
    description="Public endpoint (no authorization in v1). [AUTH_DEFERRED]",
    responses={
        404: {
            "description": "Cart not found",
            "content": {
                "application/json": {"example": {"detail": "Cart not found"}}
            },
        }
    },
)
def get_cart(cart_id: str) -> CartResponse:
    doc = carts_collection.find_one({"_id": cart_id})
    if doc is None:
        raise HTTPException(status_code=404, detail="Cart not found")
    return _to_cart_response(doc["_id"], doc["lines"], doc["total_minor_units"])


@router.post(
    "/cart/{cart_id}/items",
    response_model=CartResponse,
    summary="Add an item to a cart",
    description=(
        "Public endpoint (no authorization in v1). [AUTH_DEFERRED] "
        "Catalog is called over HTTP to check sellability and read the price. "
        "[CATALOG_URL_HARDCODED] [HTTP_TIMEOUT_DEFAULT] [NO_RETRY] "
        "[IDEMPOTENCY_DEFERRED] a repeated add increments quantity. "
        "[PRICE_DRIFT_DEFERRED] the price snapshot is set only on the first add. "
        "[MAX_QUANTITY_DEFERRED]"
    ),
    responses={
        400: {
            "model": ErrorResponse,
            "description": "Product not found or not sellable",
        },
        404: {
            "description": "Cart not found",
            "content": {
                "application/json": {"example": {"detail": "Cart not found"}}
            },
        },
        500: {
            "description": "Catalog service unavailable",
            "content": {
                "application/json": {
                    "example": {"detail": "Catalog service unavailable"}
                }
            },
        },
    },
)
async def add_item(cart_id: str, body: AddItemRequest) -> CartResponse:
    doc = carts_collection.find_one({"_id": cart_id})
    if doc is None:
        raise HTTPException(status_code=404, detail="Cart not found")

    # [CATALOG_URL_HARDCODED] [HTTP_TIMEOUT_DEFAULT] default httpx timeout is 5s.
    # [NO_RETRY] a catalog failure is returned immediately.
    url = f"{CATALOG_BASE_URL}/products/{body.product_id}"
    try:
        async with httpx.AsyncClient() as client:
            catalog_response = await client.get(url)
    except httpx.RequestError:
        raise HTTPException(
            status_code=500, detail="Catalog service unavailable"
        ) from None

    if catalog_response.status_code == 404:
        return _error(
            error="Product not found",
            reason="Product does not exist in catalog",
        )
    if catalog_response.status_code != 200:
        raise HTTPException(status_code=500, detail="Catalog service unavailable")

    catalog_product = catalog_response.json()
    if catalog_product["sellable"] is False:
        return _error(
            error="Product cannot be added",
            reason="Product is not sellable",
        )

    lines = doc["lines"]
    existing_line = next(
        (line for line in lines if line["product_id"] == body.product_id),
        None,
    )
    if existing_line:
        # Keep the price captured on the first add. [PRICE_DRIFT_DEFERRED]
        existing_line["quantity"] += body.quantity
    else:
        lines.append(
            {
                "product_id": body.product_id,
                "quantity": body.quantity,
                "price_snapshot_minor_units": catalog_product["price_minor_units"],
            }
        )

    total_minor_units = _total_minor_units(lines)
    carts_collection.update_one(
        {"_id": cart_id},
        {"$set": {"lines": lines, "total_minor_units": total_minor_units}},
    )
    return _to_cart_response(cart_id, lines, total_minor_units)


@router.patch(
    "/cart/{cart_id}/items/{product_id}",
    response_model=CartResponse,
    summary="Update an item quantity",
    description=(
        "Public endpoint (no authorization in v1). [AUTH_DEFERRED] "
        "Quantity zero removes the line. "
        "[PRICE_DRIFT_DEFERRED] this does not call catalog or change the price snapshot."
    ),
    responses={
        404: {
            "description": "Cart or product not found",
            "content": {
                "application/json": {"example": {"detail": "Product not in cart"}}
            },
        }
    },
)
def update_item(
    cart_id: str, product_id: str, body: UpdateItemRequest
) -> CartResponse:
    doc = carts_collection.find_one({"_id": cart_id})
    if doc is None:
        raise HTTPException(status_code=404, detail="Cart not found")

    lines = doc["lines"]
    existing_line = next(
        (line for line in lines if line["product_id"] == product_id),
        None,
    )
    if existing_line is None:
        raise HTTPException(status_code=404, detail="Product not in cart")

    if body.quantity == 0:
        lines.remove(existing_line)
    else:
        existing_line["quantity"] = body.quantity

    total_minor_units = _total_minor_units(lines)
    carts_collection.update_one(
        {"_id": cart_id},
        {"$set": {"lines": lines, "total_minor_units": total_minor_units}},
    )
    return _to_cart_response(cart_id, lines, total_minor_units)


@router.delete(
    "/cart/{cart_id}",
    status_code=204,
    summary="Delete a cart",
    description="Public endpoint (no authorization in v1). [AUTH_DEFERRED]",
    responses={
        404: {
            "description": "Cart not found",
            "content": {
                "application/json": {"example": {"detail": "Cart not found"}}
            },
        }
    },
)
def delete_cart(cart_id: str) -> Response:
    result = carts_collection.delete_one({"_id": cart_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Cart not found")
    return Response(status_code=204)
