from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, HTTPException, Query

from db import products_collection
from models import Product, ProductSearchResponse

# All catalog endpoints are public in v1. [AUTH_DEFERRED]
# [LOGGING_DEFERRED] no structured logging in v1.
router = APIRouter()


def _to_product(doc: dict) -> Product:
    return Product(
        id=str(doc["_id"]),
        name=doc["name"],
        price_minor_units=doc["price_minor_units"],
        sellable=doc["sellable"],
    )


@router.get(
    "/products/search",
    response_model=ProductSearchResponse,
    summary="Search products by name",
    description=(
        "Public endpoint (no authorization in v1). [AUTH_DEFERRED] "
        "Case-insensitive match on the product name. "
        "[SEARCH_RANKING_DEFERRED] results are returned in insertion order. "
        "[PAGINATION_DEFERRED] every match is returned. "
        "[FULLTEXT_DEFERRED] matching uses a regex, not full-text search."
    ),
    responses={
        400: {
            "description": "Missing or empty query",
            "content": {
                "application/json": {
                    "example": {"detail": "Query parameter 'q' is required"}
                }
            },
        }
    },
)
def search_products(q: str | None = Query(default=None)) -> ProductSearchResponse:
    if q is None or q == "":
        raise HTTPException(
            status_code=400, detail="Query parameter 'q' is required"
        )
    # [SEARCH_RANKING_DEFERRED] [PAGINATION_DEFERRED] [FULLTEXT_DEFERRED]
    query = {"name": {"$regex": q, "$options": "i"}}
    products = [_to_product(doc) for doc in products_collection.find(query)]
    return ProductSearchResponse(products=products)


@router.get(
    "/products/{product_id}",
    response_model=Product,
    summary="Fetch a product",
    description=(
        "Public endpoint (no authorization in v1). [AUTH_DEFERRED] "
        "Returns the product name, price in minor units, and sellable flag. "
        "An unsellable product still returns its price; the cart service refuses add-to-cart."
    ),
    responses={
        400: {
            "description": "Invalid ObjectId",
            "content": {
                "application/json": {"example": {"detail": "Invalid ObjectId"}}
            },
        },
        404: {
            "description": "Product not found",
            "content": {
                "application/json": {"example": {"detail": "Product not found"}}
            },
        },
    },
)
def get_product(product_id: str) -> Product:
    try:
        oid = ObjectId(product_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid ObjectId") from None
    doc = products_collection.find_one({"_id": oid})
    if doc is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return _to_product(doc)
