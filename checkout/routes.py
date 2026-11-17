import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, HTTPException
from pymongo.errors import PyMongoError

from db import checkouts_collection, outbox_collection
from models import (
    CheckoutCreateResponse,
    CheckoutLine,
    CheckoutRecord,
    CheckoutRequest,
    CheckoutStatusResponse,
    OutboxRow,
)
from outbox import insert_outbox

# Public, no auth. Any client can check out any cart. [AUTH_DEFERRED]
# Checkout does not check that cart_id belongs to shopper_id. [CART_OWNERSHIP_DEFERRED]
# [SERVICE_URLS_HARDCODED] future: CART_SERVICE_URL, PAYMENT_SERVICE_URL.
# [CONFIG_DEFERRED] service URLs are not read from the environment in v1.
# [HTTP_TIMEOUT_DEFAULT] httpx default timeout is 5s.
# [NO_RETRY] a downstream failure is returned immediately.
# [IDEMPOTENCY_DEFERRED] concurrent submits for one cart can both pass the lookup.
# [ATOMIC_WRITE_DEFERRED] pending checkout and outbox are two writes; a crash between them
# leaves an accepted charge with no message to send.
# [NO_TRANSACTIONS] partial failure is not atomic.
# [EMAIL_DEFERRED] checkout does not send a confirmation email.
# [STOCK_RESERVATION_DEFERRED] checkout does not reserve inventory.
# [FRAUD_DETECTION_DEFERRED] payment accept/refuse is not preceded by a fraud check.
# [ERROR_DETAIL_DEFERRED] 500 responses are not sanitized.
# [LOGGING_DEFERRED] print statements only in v1.
CART_SERVICE_URL = "http://localhost:8003"
PAYMENT_SERVICE_URL = "http://localhost:8005"

# A repeated checkout for these statuses returns the existing checkout and does not charge again.
_IDEMPOTENT_STATUSES = ["accepted", "order_created", "cart_cleared"]

router = APIRouter()


def _checkout_lines(lines: list) -> list:
    return [
        CheckoutLine(
            product_id=line["product_id"],
            quantity=line["quantity"],
            price_snapshot_minor_units=line["price_snapshot_minor_units"],
        )
        for line in lines
    ]


def _payload_lines(lines: list) -> list:
    # OrderRequested carries price_minor_units. The checkout record keeps the cart snapshot name.
    return [
        {
            "product_id": line["product_id"],
            "quantity": line["quantity"],
            "price_minor_units": line["price_snapshot_minor_units"],
        }
        for line in lines
    ]


@router.post(
    "/checkout",
    response_model=CheckoutCreateResponse,
    response_model_exclude_none=True,
    summary="Check out a cart",
    description=(
        "Public, no auth. [AUTH_DEFERRED] "
        "Charges the cart total just returned by the cart service. "
        "When payment accepts, stores the checkout and an unpublished "
        "OrderRequested outbox row, then returns the checkout id. "
        "The order is created asynchronously. "
        "[CART_OWNERSHIP_DEFERRED] [SERVICE_URLS_HARDCODED] "
        "[HTTP_TIMEOUT_DEFAULT] [NO_RETRY] [IDEMPOTENCY_DEFERRED] "
        "[ATOMIC_WRITE_DEFERRED] [NO_TRANSACTIONS]"
    ),
    responses={
        400: {
            "description": "Guest email missing or cart not found",
            "content": {
                "application/json": {
                    "examples": {
                        "guest_email": {
                            "value": {
                                "detail": "Email is required for guest checkout"
                            }
                        },
                        "cart_missing": {"value": {"detail": "Cart not found"}},
                    }
                }
            },
        },
        500: {
            "description": "Cart, payment, or order service unavailable",
            "content": {
                "application/json": {
                    "example": {"detail": "Cart service unavailable"}
                }
            },
        },
    },
)
async def checkout(body: CheckoutRequest) -> CheckoutCreateResponse:
    if body.shopper_id is None and body.email is None:
        raise HTTPException(
            status_code=400, detail="Email is required for guest checkout"
        )

    cart_url = f"{CART_SERVICE_URL}/cart/{body.cart_id}"
    # [HTTP_TIMEOUT_DEFAULT] [NO_RETRY] [SERVICE_URLS_HARDCODED]
    try:
        async with httpx.AsyncClient() as client:
            cart_response = await client.get(cart_url)
    except httpx.RequestError:
        raise HTTPException(
            status_code=500, detail="Cart service unavailable"
        ) from None

    if cart_response.status_code == 404:
        raise HTTPException(status_code=400, detail="Cart not found")
    if cart_response.status_code != 200:
        raise HTTPException(status_code=500, detail="Cart service unavailable")

    cart_data = cart_response.json()
    # Price authority stays with the cart. Do not reprice the lines.
    cart_total = cart_data["total_minor_units"]
    cart_lines = cart_data["lines"]

    # [IDEMPOTENCY_DEFERRED] two concurrent submits can both pass this lookup.
    existing_checkout = checkouts_collection.find_one(
        {
            "cart_id": body.cart_id,
            "status": {"$in": _IDEMPOTENT_STATUSES},
        }
    )
    if existing_checkout:
        return CheckoutCreateResponse(
            checkout_id=existing_checkout["checkout_id"],
            status="accepted",
        )

    checkout_id = str(uuid.uuid4())
    payment_reference = str(uuid.uuid4())
    now = datetime.utcnow()
    email = str(body.email) if body.email else None

    pending_checkout = CheckoutRecord(
        checkout_id=checkout_id,
        payment_reference=payment_reference,
        cart_id=body.cart_id,
        shopper_id=body.shopper_id,
        email=email,
        ship_to=body.ship_to,
        lines=_checkout_lines(cart_lines),
        total_minor_units=cart_total,
        status="pending",
        order_id=None,
        created_at=now,
        updated_at=now,
    )
    try:
        checkouts_collection.insert_one(pending_checkout.model_dump(by_alias=True))
    except PyMongoError:
        raise HTTPException(
            status_code=500, detail="Failed to store checkout"
        ) from None

    try:
        async with httpx.AsyncClient() as client:
            payment_response = await client.post(
                f"{PAYMENT_SERVICE_URL}/charge",
                json={
                    "amount_minor_units": cart_total,
                    "payment_reference": payment_reference,
                },
            )
    except httpx.RequestError:
        checkouts_collection.delete_one({"checkout_id": checkout_id})
        raise HTTPException(
            status_code=500, detail="Payment service unavailable"
        ) from None

    if payment_response.status_code != 200:
        checkouts_collection.delete_one({"checkout_id": checkout_id})
        raise HTTPException(status_code=500, detail="Payment service unavailable")

    payment_data = payment_response.json()
    if payment_data["status"] == "refused":
        checkouts_collection.update_one(
            {"checkout_id": checkout_id},
            {"$set": {"status": "refused", "updated_at": datetime.utcnow()}},
        )
        return CheckoutCreateResponse(
            checkout_id=checkout_id,
            status="refused",
            reason=payment_data["reason"],
        )

    # [ATOMIC_WRITE_DEFERRED] outbox is a second write. Roll back the pending checkout on failure.
    outbox_row = OutboxRow(
        event_type="OrderRequested",
        payload={
            "event_type": "OrderRequested",
            "payment_reference": payment_reference,
            "checkout_id": checkout_id,
            "shopper_id": body.shopper_id,
            "email": email,
            "ship_to": body.ship_to.model_dump(),
            "lines": _payload_lines(cart_lines),
            "total_minor_units": cart_total,
            "timestamp": datetime.utcnow().isoformat(),
        },
        payment_reference=payment_reference,
        published=False,
        published_at=None,
        created_at=datetime.utcnow(),
    )
    try:
        insert_outbox(outbox_row)
    except PyMongoError as exc:
        print(f"Failed to store outbox for checkout {checkout_id}: {exc}")
        checkouts_collection.delete_one({"checkout_id": checkout_id})
        raise HTTPException(status_code=500, detail="Failed to store outbox") from None

    checkouts_collection.update_one(
        {"checkout_id": checkout_id},
        {"$set": {"status": "accepted", "updated_at": datetime.utcnow()}},
    )
    return CheckoutCreateResponse(checkout_id=checkout_id, status="accepted")


@router.get(
    "/checkout/{checkout_id}",
    response_model=CheckoutStatusResponse,
    summary="Fetch checkout status",
    description=(
        "Public endpoint (no authorization in v1). [AUTH_DEFERRED] "
        "Returns the checkout status, order id (when the order has been created), "
        "and created timestamp. "
        "The status is one of: accepted (checkout stored, order not yet created), "
        "order_created (order exists but cart not yet cleared), "
        "cart_cleared (happy path complete), "
        "or refunded (order never appeared, charge refunded)."
    ),
    responses={
        404: {
            "description": "Checkout not found",
            "content": {
                "application/json": {"example": {"detail": "Checkout not found"}}
            },
        },
        500: {
            "description": "Database query failed",
            "content": {
                "application/json": {"example": {"detail": "Database query failed"}}
            },
        },
    },
)
def get_checkout_status(checkout_id: str) -> CheckoutStatusResponse:
    try:
        checkout = checkouts_collection.find_one({"checkout_id": checkout_id})
    except PyMongoError:
        # [ERROR_DETAIL_DEFERRED] fixed message; the response has no stack trace.
        print(
            f"[ERROR_DETAIL_DEFERRED] Database query failed for checkout_id {checkout_id}"
        )
        raise HTTPException(status_code=500, detail="Database query failed") from None

    if checkout is None:
        raise HTTPException(status_code=404, detail="Checkout not found")

    # [BACKFILL_DEFERRED] status is missing for checkouts created before Story 1.
    status = checkout.get("status", "accepted")

    created_at = checkout.get("created_at")
    if created_at is None:
        # [BACKFILL_DEFERRED] created_at is missing for checkouts created before Story 1.
        created_at_str = "1970-01-01T00:00:00Z"
    else:
        created_at_str = created_at.isoformat()

    return CheckoutStatusResponse(
        checkout_id=checkout["checkout_id"],
        status=status,
        order_id=checkout.get("order_id"),
        created_at=created_at_str,
    )
