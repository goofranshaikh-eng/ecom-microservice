import os

from pymongo import MongoClient

# Charge documents live only in payment_db.charges. This service never opens
# another service's database and never calls another service.
#   _id: ObjectId
#   amount_minor_units: integer (> 0)
#   status: "accepted" | "refused"
#   reference: string (UUID, only when accepted)
#   reason: string (only when refused)
#   timestamp: ISODate (UTC)
# [CURRENCY_DEFERRED] no currency field.
# [MERCHANT_DEFERRED] no merchant_id.
# [CHARGE_METADATA_DEFERRED] no metadata for cart, order, or shopper.
# [CHARGE_EXPIRY_DEFERRED] records are kept indefinitely; no TTL index.
# [IDEMPOTENCY_DEFERRED] no idempotency key and no unique index for it.
# [CONFIG_DEFERRED] URI comes from the environment; no .env loading.
# No pooling overrides and no retry; MongoClient defaults, fail fast.

MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")

client = MongoClient(MONGODB_URI)
db = client["payment_db"]
charges_collection = db["charges"]
# Optional lookup and audit indexes. Primary key remains _id.
charges_collection.create_index("reference")
charges_collection.create_index("timestamp")
