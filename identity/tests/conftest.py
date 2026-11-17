import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

shoppers_collection = MagicMock(name="shoppers")
identity_db = MagicMock(name="identity_db")
identity_db.__getitem__.return_value = shoppers_collection
mongo_client = MagicMock(name="MongoClient")
mongo_client.__getitem__.return_value = identity_db

patch("pymongo.MongoClient", return_value=mongo_client).start()
