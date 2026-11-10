from typing import Literal, Optional

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


class CheckoutResponse(BaseModel):
    order_id: Optional[str] = None
    status: Literal["paid", "refused"]
    reason: Optional[str] = None

    @model_validator(mode="after")
    def require_status_fields(self):
        if self.status == "paid" and self.order_id is None:
            raise ValueError("order_id must be present when status is paid")
        if self.status == "refused" and self.reason is None:
            raise ValueError("reason must be present when status is refused")
        return self
