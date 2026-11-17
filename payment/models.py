from typing import Literal, Optional

from pydantic import BaseModel, field_validator, model_validator

# Charge document in payment_db.charges:
#   _id: ObjectId
#   reference: str (UUID)
#   amount_minor_units: int
#   card: str | None
#   status: "accepted" | "refused" | "refunded"
#   refunded_at: datetime | None  # set when status is "refunded"
#   reason: str | None (for refused charges)
#   timestamp: datetime


class ChargeRequest(BaseModel):
    amount_minor_units: int
    card: Optional[str] = None

    @field_validator("amount_minor_units")
    @classmethod
    def amount_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Amount must be positive")
        return value


class ChargeResponse(BaseModel):
    status: Literal["accepted", "refused", "refunded"]
    reference: Optional[str] = None
    reason: Optional[str] = None

    @model_validator(mode="after")
    def require_status_fields(self):
        if self.status == "accepted" and self.reference is None:
            raise ValueError("reference must be present when status is accepted")
        if self.status == "refused" and self.reason is None:
            raise ValueError("reason must be present when status is refused")
        # [NOTE] refunded charges have a reference but no reason
        return self
