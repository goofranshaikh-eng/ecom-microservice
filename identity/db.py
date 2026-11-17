import os

from pymongo import MongoClient

# Shopper documents live only in identity_db.shoppers:
#   _id: ObjectId
#   email: string (unique)
#   password: string  # [STUB_AUTH] plaintext in v1; replace with password_hash later

MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")

client = MongoClient(MONGODB_URI)
db = client["identity_db"]
shoppers_collection = db["shoppers"]
shoppers_collection.create_index("email", unique=True)
