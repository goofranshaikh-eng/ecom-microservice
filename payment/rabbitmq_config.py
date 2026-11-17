import os

import pika

RABBITMQ_URI = os.environ.get("RABBITMQ_URI", "amqp://localhost:5672")
EXCHANGE_NAME = "ecom.events"


def get_rabbitmq_connection() -> pika.BlockingConnection:
    """
    Return a RabbitMQ connection from RABBITMQ_URI.
    Default: amqp://localhost:5672. [CONFIG_DEFERRED]
    """
    return pika.BlockingConnection(pika.URLParameters(RABBITMQ_URI))
