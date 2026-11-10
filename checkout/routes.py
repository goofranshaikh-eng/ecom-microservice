import httpx
from fastapi import APIRouter, HTTPException

from models import CheckoutRequest, CheckoutResponse

# Public, no auth. Any client can check out any cart. [AUTH_DEFERRED]
# Checkout does not check that cart_id belongs to shopper_id. [CART_OWNERSHIP_DEFERRED]
# [SERVICE_URLS_HARDCODED] future: CART_SERVICE_URL, PAYMENT_SERVICE_URL, ORDER_SERVICE_URL.
# [CONFIG_DEFERRED] service URLs are not read from the environment in v1.
# [HTTP_TIMEOUT_DEFAULT] httpx default timeout is 5s.
# [NO_RETRY] a downstream failure is returned immediately.
# [IDEMPOTENCY_DEFERRED] a repeated checkout can charge again and create another order.
# [NO_TRANSACTIONS] partial failure is not atomic.
# [EMAIL_DEFERRED] checkout does not send a confirmation email.
# [STOCK_RESERVATION_DEFERRED] checkout does not reserve inventory.
# [FRAUD_DETECTION_DEFERRED] payment accept/refuse is not preceded by a fraud check.
# [ERROR_DETAIL_DEFERRED] 500 responses are not sanitized.
# [LOGGING_DEFERRED] print statements only in v1.
CART_SERVICE_URL = "http://localhost:8003"
PAYMENT_SERVICE_URL = "http://localhost:8005"
ORDER_SERVICE_URL = "http://localhost:8006"

router = APIRouter()


def _log_cart_delete_failure(cart_id: str) -> None:
    # [KNOWN_GAP] Order created but cart not emptied.
    print(f"Failed to delete cart {cart_id} after checkout")


def _order_payload(
    body: CheckoutRequest, lines: list, total_minor_units: int, payment_reference: str
) -> dict:
    payload = {
        "ship_to": body.ship_to.model_dump(),
        "lines": lines,
        "total_minor_units": total_minor_units,
        "payment_reference": payment_reference,
    }
    if body.shopper_id is not None:
        payload["shopper_id"] = body.shopper_id
    if body.email is not None:
        payload["email"] = str(body.email)
    return payload


@router.post(
    "/checkout",
    response_model=CheckoutResponse,
    response_model_exclude_none=True,
    summary="Check out a cart",
    description=(
        "Public, no auth. [AUTH_DEFERRED] "
        "Charges the cart total just returned by the cart service. "
        "When payment accepts, stores an order and empties the cart. "
        "[CART_OWNERSHIP_DEFERRED] [SERVICE_URLS_HARDCODED] "
        "[HTTP_TIMEOUT_DEFAULT] [NO_RETRY] [IDEMPOTENCY_DEFERRED] "
        "[NO_TRANSACTIONS] [KNOWN_GAP]"
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
async def checkout(body: CheckoutRequest) -> CheckoutResponse:
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

    try:
        async with httpx.AsyncClient() as client:
            payment_response = await client.post(
                f"{PAYMENT_SERVICE_URL}/charge",
                json={"amount_minor_units": cart_total},
            )
    except httpx.RequestError:
        raise HTTPException(
            status_code=500, detail="Payment service unavailable"
        ) from None

    if payment_response.status_code != 200:
        raise HTTPException(status_code=500, detail="Payment service unavailable")

    payment_data = payment_response.json()
    if payment_data["status"] == "refused":
        return CheckoutResponse(status="refused", reason=payment_data["reason"])

    order_payload = _order_payload(
        body, cart_lines, cart_total, payment_data["reference"]
    )
    try:
        async with httpx.AsyncClient() as client:
            order_response = await client.post(
                f"{ORDER_SERVICE_URL}/orders",
                json=order_payload,
            )
    except httpx.RequestError:
        # [KNOWN_GAP] Payment succeeded but order not created. The charge is not reversed.
        raise HTTPException(
            status_code=500, detail="Order service unavailable"
        ) from None

    if order_response.status_code != 201:
        # [KNOWN_GAP] Payment succeeded but order not created. The charge is not reversed.
        raise HTTPException(status_code=500, detail="Order service unavailable")

    order_id = order_response.json()["order_id"]

    try:
        async with httpx.AsyncClient() as client:
            delete_response = await client.delete(cart_url)
    except httpx.RequestError:
        _log_cart_delete_failure(body.cart_id)
    else:
        if delete_response.status_code != 204:
            _log_cart_delete_failure(body.cart_id)

    return CheckoutResponse(order_id=order_id, status="paid")
