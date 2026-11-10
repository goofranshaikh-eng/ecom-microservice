import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

carts_collection = MagicMock(name="carts")
cart_db = MagicMock(name="cart_db")
cart_db.__getitem__.return_value = carts_collection
mongo_client = MagicMock(name="MongoClient")
mongo_client.__getitem__.return_value = cart_db

patch("pymongo.MongoClient", return_value=mongo_client).start()
