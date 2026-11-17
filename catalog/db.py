import os

from pymongo import MongoClient

# Product documents live only in catalog_db.products. This service never opens
# another service's database.
#   _id: ObjectId
#   name: string
#   price_minor_units: integer
#   sellable: boolean
# [CURRENCY_DEFERRED] price is a single implicit currency; no currency field.
# [INVENTORY_DEFERRED] sellable is a flag; there is no stock quantity.
# [CATEGORIES_DEFERRED] no categories or tags.
# [IMAGES_DEFERRED] no image_url.
# [CATALOG_MANAGEMENT_DEFERRED] no create, update, or delete in v1.

MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")

# [CONFIG_DEFERRED] URI and PORT come from the environment; no .env loading.
client = MongoClient(MONGODB_URI)
db = client["catalog_db"]
products_collection = db["products"]
# [FULLTEXT_DEFERRED] optional text index; v1 search uses a case-insensitive regex.
products_collection.create_index([("name", "text")])
