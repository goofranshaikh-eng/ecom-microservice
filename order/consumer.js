const Joi = require('joi');

const db = require('./db');
const {
  EXCHANGE_NAME,
  closeRabbitMQ,
  connectRabbitMQ,
  publishMessage,
  setupExchangeAndQueue,
} = require('./rabbitmq');

// Order reads and writes only order_db.orders. It does not open another database.
const QUEUE_NAME = 'order.requested.queue';
const ROUTING_KEY = 'order.requested';
const ORDER_CREATED_QUEUE = 'order.created.queue';
const ORDER_CREATED_ROUTING_KEY = 'order.created';

const orderRequestedSchema = Joi.object({
  event_type: Joi.string().valid('OrderRequested').required(),
  payment_reference: Joi.string().trim().min(1).required(),
  checkout_id: Joi.string().trim().min(1).required(),
  shopper_id: Joi.string().trim().min(1),
  email: Joi.string().trim().min(1),
  ship_to: Joi.object({
    street: Joi.string().trim().min(1).required(),
    city: Joi.string().trim().min(1).required(),
    postal_code: Joi.string().trim().min(1).required(),
    country: Joi.string().trim().min(1).required(),
  }).required(),
  lines: Joi.array()
    .items(
      Joi.object({
        product_id: Joi.string().trim().min(1).required(),
        quantity: Joi.number().integer().positive().required(),
        price_snapshot_minor_units: Joi.number().integer().min(0).required(),
      }),
    )
    .min(1)
    .required(),
  total_minor_units: Joi.number().integer().min(0).required(),
  timestamp: Joi.string().isoDate().required(),
});

function buildOrderDoc(msg) {
  const orderDoc = {
    payment_reference: msg.payment_reference,
    ship_to: msg.ship_to,
    lines: msg.lines.map((line) => ({
      product_id: line.product_id,
      quantity: line.quantity,
      price_minor_units: line.price_snapshot_minor_units,
    })),
    total_minor_units: msg.total_minor_units,
    created_at: new Date(),
  };

  if (msg.shopper_id) {
    orderDoc.shopper_id = msg.shopper_id;
  }
  if (msg.email) {
    orderDoc.email = msg.email;
  }

  return orderDoc;
}

async function publishOrderCreated(channel, msg, orderId, publish) {
  const payload = {
    event_type: 'OrderCreated',
    payment_reference: msg.payment_reference,
    checkout_id: msg.checkout_id,
    order_id: orderId,
    timestamp: new Date().toISOString(),
  };

  try {
    await publish(channel, EXCHANGE_NAME, ORDER_CREATED_ROUTING_KEY, payload);
    console.log(
      `Published OrderCreated for payment_reference ${msg.payment_reference}`,
    );
  } catch (err) {
    console.error('Failed to publish OrderCreated:', err);
    const failure = new Error('Failed to publish OrderCreated');
    failure.cause = err;
    failure.isPublishFailure = true;
    throw failure;
  }
}

function nack(channel, rabbitMsg, requeue) {
  channel.nack(rabbitMsg, false, requeue);
}

async function handleOrderRequested(channel, rabbitMsg, deps = {}) {
  if (!rabbitMsg) {
    return;
  }

  const orders = () =>
    deps.getOrdersCollection
      ? deps.getOrdersCollection()
      : db.getOrdersCollection();
  const publish = deps.publishMessage || publishMessage;

  let msg;
  try {
    msg = JSON.parse(rabbitMsg.content.toString());
  } catch (err) {
    console.error('Failed to parse message:', err);
    nack(channel, rabbitMsg, false);
    return;
  }

  const { error, value } = orderRequestedSchema.validate(msg, {
    abortEarly: false,
  });
  if (error) {
    console.error('Invalid OrderRequested message:', error.details[0].message);
    nack(channel, rabbitMsg, false);
    return;
  }

  const orderDoc = buildOrderDoc(value);

  try {
    const existingOrder = await orders().findOne({
      payment_reference: value.payment_reference,
    });

    if (existingOrder) {
      console.log(
        `Order already exists for payment_reference ${value.payment_reference}, skipping insert`,
      );
      await publishOrderCreated(
        channel,
        value,
        existingOrder._id.toString(),
        publish,
      );
      channel.ack(rabbitMsg);
      return;
    }

    try {
      const result = await orders().insertOne(orderDoc);
      await publishOrderCreated(
        channel,
        value,
        result.insertedId.toString(),
        publish,
      );
      channel.ack(rabbitMsg);
    } catch (err) {
      if (err.code === 11000) {
        const existing = await orders().findOne({
          payment_reference: value.payment_reference,
        });
        console.log(
          `Duplicate key error handled for payment_reference ${value.payment_reference}`,
        );
        await publishOrderCreated(
          channel,
          value,
          existing._id.toString(),
          publish,
        );
        channel.ack(rabbitMsg);
      } else if (err.isPublishFailure) {
        nack(channel, rabbitMsg, true);
      } else {
        console.error('Database error:', err);
        nack(channel, rabbitMsg, true);
      }
    }
  } catch (err) {
    if (!err.isPublishFailure) {
      console.error('Database error:', err);
    }
    nack(channel, rabbitMsg, true);
  }
}

async function consumeOrderRequested() {
  let connected;
  try {
    connected = await connectRabbitMQ();
  } catch (err) {
    process.exit(1);
  }

  const { connection, channel } = connected;

  connection.on('error', (err) => {
    console.error('RabbitMQ connection error:', err);
    process.exit(1);
  });
  channel.on('error', (err) => {
    console.error('RabbitMQ channel error:', err);
    closeRabbitMQ().finally(() => process.exit(1));
  });

  await setupExchangeAndQueue(channel, EXCHANGE_NAME, QUEUE_NAME, ROUTING_KEY);
  await setupExchangeAndQueue(
    channel,
    EXCHANGE_NAME,
    ORDER_CREATED_QUEUE,
    ORDER_CREATED_ROUTING_KEY,
  );
  await channel.prefetch(10);

  console.log('RabbitMQ connected, exchange and queue declared');

  await channel.consume(
    QUEUE_NAME,
    (rabbitMsg) => {
      handleOrderRequested(channel, rabbitMsg).catch((err) => {
        console.error('Database error:', err);
        if (rabbitMsg) {
          nack(channel, rabbitMsg, true);
        }
      });
    },
    { noAck: false },
  );
}

module.exports = {
  consumeOrderRequested,
  handleOrderRequested,
  orderRequestedSchema,
};
