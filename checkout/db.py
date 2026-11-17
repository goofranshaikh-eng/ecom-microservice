import os

from pymongo import MongoClient

# Checkout owns checkout_db only. Collections: checkouts, outbox.
# [ATOMIC_WRITE_DEFERRED] checkout and outbox are separate writes until replica-set transactions exist.
# [IDEMPOTENCY_DEFERRED] concurrent submits for one cart can both pass the lookup.
# No pooling overrides and no retry; MongoClient defaults, fail fast.
# The database is created on first write.
# [CONFIG_DEFERRED] URI comes from the environment; no .env loading.

MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")

client = MongoClient(MONGODB_URI)
db = client["checkout_db"]
checkouts_collection = db["checkouts"]
outbox_collection = db["outbox"]
