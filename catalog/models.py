from typing import List

from pydantic import BaseModel, Field


class Product(BaseModel):
    id: str
    name: str = Field(..., min_length=1)
    price_minor_units: int = Field(..., ge=0)
    sellable: bool


class ProductSearchResponse(BaseModel):
    products: List[Product]
