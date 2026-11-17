from datetime import datetime

from bson import ObjectId

from db import outbox_collection
from models import OutboxRow


def insert_outbox(row: OutboxRow) -> None:
    """
    Insert an outbox row to outbox_collection.
    Raises PyMongoError on failure.
    """
    outbox_collection.insert_one(row.model_dump(by_alias=True))


def mark_published(row_id: ObjectId) -> None:
    """
    Mark an outbox row as published.
    Sets published = True and published_at = now().
    Raises PyMongoError on failure.
    """
    outbox_collection.update_one(
        {"_id": row_id},
        {"$set": {"published": True, "published_at": datetime.utcnow()}},
    )
