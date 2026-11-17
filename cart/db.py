import os

from pymongo import MongoClient

# Cart documents live only in cart_db.carts. This service never opens
# another service's database. Catalog price and sellability are read over HTTP.
#   _id: string (cart_id)
#   lines: [{product_id, quantity, price_snapshot_minor_units}]
#   total_minor_units: integer
# [CART_EXPIRY_DEFERRED] no created_at and no TTL index.
# [CART_OWNERSHIP_DEFERRED] no shopper_id.
# [CONCURRENT_UPDATES_DEFERRED] last write wins; no version field.
# [CART_DISPLAY_DEFERRED] lines do not store a product name or image.
# Primary key is _id. No additional indexes in v1.

MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")

# [CONFIG_DEFERRED] URI and PORT come from the environment; no .env loading.
client = MongoClient(MONGODB_URI)
db = client["cart_db"]
carts_collection = db["carts"]
