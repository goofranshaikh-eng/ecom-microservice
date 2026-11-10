import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

products_collection = MagicMock(name="products")
catalog_db = MagicMock(name="catalog_db")
catalog_db.__getitem__.return_value = products_collection
mongo_client = MagicMock(name="MongoClient")
mongo_client.__getitem__.return_value = catalog_db

patch("pymongo.MongoClient", return_value=mongo_client).start()
