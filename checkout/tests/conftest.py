import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

checkout_db = MagicMock(name="checkout_db")
_collections: dict = {}


def _collection(name):
    if name not in _collections:
        _collections[name] = MagicMock(name=name)
    return _collections[name]


checkout_db.__getitem__.side_effect = _collection
mongo_client = MagicMock(name="MongoClient")
mongo_client.__getitem__.return_value = checkout_db

patch("pymongo.MongoClient", return_value=mongo_client).start()
