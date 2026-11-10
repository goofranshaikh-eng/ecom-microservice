import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

charges_collection = MagicMock(name="charges")
payment_db = MagicMock(name="payment_db")
payment_db.__getitem__.return_value = charges_collection
mongo_client = MagicMock(name="MongoClient")
mongo_client.__getitem__.return_value = payment_db

patch("pymongo.MongoClient", return_value=mongo_client).start()
