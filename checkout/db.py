import os

from pymongo import MongoClient

# Checkout owns checkout_db only. v1 is stateless and uses no collections.
# Future idempotency keys and audit logs may be stored in this database.
# [IDEMPOTENCY_DEFERRED] [AUDIT_LOG_DEFERRED]
# No pooling overrides and no retry; MongoClient defaults, fail fast.
# The database is created on first write.
# [CONFIG_DEFERRED] URI comes from the environment; no .env loading.

MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")

client = MongoClient(MONGODB_URI)
db = client["checkout_db"]
