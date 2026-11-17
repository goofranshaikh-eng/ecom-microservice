from typing import List

from pydantic import BaseModel, Field


class CartLine(BaseModel):
    product_id: str
    quantity: int = Field(..., gt=0)
    price_snapshot_minor_units: int = Field(..., ge=0)


class CartResponse(BaseModel):
    cart_id: str
    lines: List[CartLine]
    total_minor_units: int = Field(..., ge=0)


class CreateCartResponse(BaseModel):
    cart_id: str


class AddItemRequest(BaseModel):
    product_id: str = Field(..., min_length=1)
    quantity: int = Field(..., gt=0)


class UpdateItemRequest(BaseModel):
    quantity: int = Field(..., ge=0)


class ErrorResponse(BaseModel):
    error: str
    reason: str
