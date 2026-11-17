from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, EmailStr, Field, model_validator


class ShipTo(BaseModel):
    street: str = Field(..., min_length=1)
    city: str = Field(..., min_length=1)
    postal_code: str = Field(..., min_length=1)
    country: str = Field(..., min_length=1)


class CheckoutRequest(BaseModel):
    cart_id: str = Field(..., min_length=1)
    shopper_id: Optional[str] = None
    email: Optional[EmailStr] = None
    ship_to: ShipTo


class CheckoutLine(BaseModel):
    product_id: str
    quantity: int
    price_snapshot_minor_units: int


class CheckoutRecord(BaseModel):
    checkout_id: str
    payment_reference: str
    cart_id: str
    shopper_id: Optional[str] = None
    email: Optional[str] = None
    ship_to: ShipTo
    lines: List[CheckoutLine]
    total_minor_units: int
    status: Literal[
        "pending",
        "accepted",
        "refused",
        "order_created",
        "cart_cleared",
        "refund_pending",
        "refunded",
    ]
    order_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class OutboxRow(BaseModel):
    event_type: Literal["OrderRequested", "RefundRequested"]
    payload: dict
    payment_reference: str
    published: bool
    published_at: Optional[datetime] = None
    created_at: datetime


class CheckoutCreateResponse(BaseModel):
    checkout_id: str
    status: Literal["accepted", "refused"]
    reason: Optional[str] = None

    @model_validator(mode="after")
    def require_reason_when_refused(self):
        if self.status == "refused" and self.reason is None:
            raise ValueError("reason must be present when status is refused")
        return self


class CheckoutStatusResponse(BaseModel):
    checkout_id: str
    status: Literal["accepted", "order_created", "cart_cleared", "refunded"]
    order_id: Optional[str] = None
    created_at: str  # ISO 8601 timestamp

    @model_validator(mode="after")
    def order_id_present_when_order_exists(self):
        if self.status in ("order_created", "cart_cleared") and self.order_id is None:
            raise ValueError(
                "order_id must be present when status is order_created or cart_cleared"
            )
        return self
