import uuid
from datetime import datetime

from bson import ObjectId
from fastapi import APIRouter, HTTPException
from pymongo.errors import PyMongoError

from db import charges_collection
from models import ChargeRequest, ChargeResponse

# Public, no auth. Any client can create a charge. [AUTH_DEFERRED]
# [TEST_DOUBLE] deterministic stub. Replace with a real payment provider later.
# [CARD_VALIDATION_DEFERRED] card is an opaque string; no Luhn, expiry, or CVV check.
# [REFUSAL_REASONS_STUB] only "Amount exceeds limit" and "Card declined".
# [IDEMPOTENCY_DEFERRED] a repeated POST /charge creates another charge record.
# [CHARGE_LOOKUP_DEFERRED] no GET /charges/{charge_id}.
# [REFUNDS_DEFERRED] no POST /refund.
# [WEBHOOKS_DEFERRED] no provider webhook receiver.
# [FRAUD_DETECTION_DEFERRED] no IP, velocity, or fraud score check.
# [CURRENCY_DEFERRED] single implicit currency; amounts stay integers.
# [MERCHANT_DEFERRED] single implicit merchant.
# [CHARGE_METADATA_DEFERRED] the charge record has no checkout context.
# [RATE_LIMITING_DEFERRED] no flood protection.
# [LOGGING_DEFERRED] no structured logging in v1.
# [ERROR_DETAIL_DEFERRED] insertion failures return a fixed 500 detail.
# [CONFIG_DEFERRED] no .env loading.

router = APIRouter()

AMOUNT_LIMIT_MINOR_UNITS = 100000


def _store_charge(doc: dict) -> None:
    try:
        charges_collection.insert_one(doc)
    except PyMongoError:
        # [ERROR_DETAIL_DEFERRED] fixed detail; the response has no stack trace.
        raise HTTPException(status_code=500, detail="Failed to store charge") from None


@router.post(
    "/charge",
    response_model=ChargeResponse,
    response_model_exclude_none=True,
    summary="Charge a payment",
    description=(
        "Public, no auth. [AUTH_DEFERRED] "
        "[TEST_DOUBLE] Accepts the charge unless the amount is at least "
        "100000 minor units or the card ends in 0000. "
        "[CARD_VALIDATION_DEFERRED] [REFUSAL_REASONS_STUB] "
        "[IDEMPOTENCY_DEFERRED] [RATE_LIMITING_DEFERRED]"
    ),
    responses={
        422: {"description": "Amount is not a positive integer"},
        500: {
            "description": "Database insertion failure",
            "content": {
                "application/json": {
                    "example": {"detail": "Failed to store charge"}
                }
            },
        },
    },
)
def charge(body: ChargeRequest) -> ChargeResponse:
    # [TEST_DOUBLE] hardcoded acceptance and refusal. No payment provider is called.
    # [FRAUD_DETECTION_DEFERRED] [CARD_VALIDATION_DEFERRED]
    refused = False
    reason = None

    if body.amount_minor_units >= AMOUNT_LIMIT_MINOR_UNITS:
        refused = True
        # [REFUSAL_REASONS_STUB]
        reason = "Amount exceeds limit"
    elif body.card is not None and body.card.endswith("0000"):
        refused = True
        # [REFUSAL_REASONS_STUB]
        reason = "Card declined"

    if refused:
        doc = {
            "_id": ObjectId(),
            "amount_minor_units": body.amount_minor_units,
            "status": "refused",
            "reason": reason,
            "timestamp": datetime.utcnow(),  # [UTC_TIMESTAMP]
        }
        _store_charge(doc)
        return ChargeResponse(status="refused", reason=reason)

    reference = str(uuid.uuid4())  # [UUID_REFERENCE]
    doc = {
        "_id": ObjectId(),
        "amount_minor_units": body.amount_minor_units,
        "status": "accepted",
        "reference": reference,
        "timestamp": datetime.utcnow(),  # [UTC_TIMESTAMP]
    }
    _store_charge(doc)
    return ChargeResponse(status="accepted", reference=reference)
