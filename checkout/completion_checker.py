import os
import time
from datetime import datetime, timedelta

from bson import ObjectId

from db import checkouts_collection, outbox_collection

# [CONFIG_DEFERRED] window and poll interval come from the environment.
COMPLETION_WINDOW_SECONDS = int(os.environ.get("COMPLETION_WINDOW_SECONDS", "300"))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))


def check_expired_checkouts() -> None:
    """
    Poll checkouts_collection for expired accepted checkouts, insert RefundRequested outbox, update status.
    Runs forever (polls every POLL_INTERVAL_SECONDS, default 30 seconds).
    """
    while True:
        cutoff = datetime.utcnow() - timedelta(seconds=COMPLETION_WINDOW_SECONDS)
        expired = checkouts_collection.find(
            {"status": "accepted", "created_at": {"$lt": cutoff}}
        )

        for checkout in expired:
            checkout_id = checkout["checkout_id"]
            payment_reference = checkout["payment_reference"]

            outbox_row = {
                "_id": ObjectId(),
                "event_type": "RefundRequested",
                "payload": {
                    "event_type": "RefundRequested",
                    "payment_reference": payment_reference,
                    "checkout_id": checkout_id,
                    "timestamp": datetime.utcnow().isoformat(),
                },
                "payment_reference": payment_reference,
                "published": False,
                "published_at": None,
                "created_at": datetime.utcnow(),
            }
            outbox_collection.insert_one(outbox_row)

            checkouts_collection.update_one(
                {"_id": checkout["_id"]},
                {
                    "$set": {
                        "status": "refund_pending",
                        "updated_at": datetime.utcnow(),
                    }
                },
            )

            print(
                f"Inserted RefundRequested for checkout {checkout_id}, payment_reference {payment_reference}"
            )

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    check_expired_checkouts()
