const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

const { ObjectId } = require('mongodb');

const { createOrderSchema } = require('../models');
const {
  consumeOrderRequested,
  handleOrderRequested,
} = require('../consumer');
const { closeRabbitMQ, publishMessage, setupExchangeAndQueue } = require('../rabbitmq');

const SERVICE_ROOT = path.join(__dirname, '..');

function orderRequested(overrides = {}) {
  return {
    event_type: 'OrderRequested',
    payment_reference: 'pay_test_1',
    checkout_id: 'chk_test_1',
    email: 'guest@example.com',
    ship_to: {
      street: '123 Main St',
      city: 'Springfield',
      postal_code: '12345',
      country: 'US',
    },
    lines: [
      {
        product_id: 'prod_001',
        quantity: 2,
        price_snapshot_minor_units: 1500,
      },
    ],
    total_minor_units: 3000,
    timestamp: new Date().toISOString(),
    ...overrides,
  };
}

function rabbitMessage(payload) {
  return { content: Buffer.from(JSON.stringify(payload)) };
}

function createCollection(seed = []) {
  const orders = seed.map((doc) => ({ ...doc }));
  return {
    orders,
    async findOne(query) {
      return (
        orders.find((doc) => doc.payment_reference === query.payment_reference) ||
        null
      );
    },
    async insertOne(doc) {
      if (
        orders.some(
          (existing) => existing.payment_reference === doc.payment_reference,
        )
      ) {
        const err = new Error('E11000 duplicate key');
        err.code = 11000;
        throw err;
      }
      const stored = { ...doc, _id: doc._id || new ObjectId() };
      orders.push(stored);
      return { insertedId: stored._id };
    },
  };
}

function createHarness(collection) {
  const published = [];
  const acks = [];
  const nacks = [];
  const channel = {
    ack(msg) {
      acks.push(msg);
    },
    nack(msg, allUpTo, requeue) {
      nacks.push({ msg, allUpTo, requeue });
    },
  };
  const deps = {
    getOrdersCollection: () => collection,
    async publishMessage(_channel, exchange, routingKey, payload) {
      published.push({ exchange, routingKey, payload });
    },
  };
  return { published, acks, nacks, channel, deps };
}

test('test_order_created_from_order_requested', async () => {
  const collection = createCollection();
  const harness = createHarness(collection);
  const payload = orderRequested();
  const rabbitMsg = rabbitMessage(payload);

  await handleOrderRequested(harness.channel, rabbitMsg, harness.deps);

  assert.equal(collection.orders.length, 1);
  const order = collection.orders[0];
  assert.equal(order.payment_reference, 'pay_test_1');
  assert.equal(order.email, 'guest@example.com');
  assert.equal(order.shopper_id, undefined);
  assert.equal(order.checkout_id, undefined);
  assert.equal(order.ship_to.street, '123 Main St');
  assert.equal(order.lines.length, 1);
  assert.equal(order.lines[0].product_id, 'prod_001');
  assert.equal(order.lines[0].quantity, 2);
  assert.equal(order.lines[0].price_minor_units, 1500);
  assert.equal(order.lines[0].price_snapshot_minor_units, undefined);
  assert.equal(order.total_minor_units, 3000);
  assert.ok(order.created_at instanceof Date);
  assert.equal(harness.acks.length, 1);
  assert.equal(harness.nacks.length, 0);
  assert.equal(harness.published.length, 1);
  assert.equal(harness.published[0].exchange, 'ecom.events');
  assert.equal(harness.published[0].routingKey, 'order.created');
  assert.equal(harness.published[0].payload.event_type, 'OrderCreated');
  assert.equal(harness.published[0].payload.order_id, order._id.toString());

  const { error } = createOrderSchema.validate({
    email: order.email,
    ship_to: order.ship_to,
    lines: order.lines,
    total_minor_units: order.total_minor_units,
    payment_reference: order.payment_reference,
  });
  assert.equal(error, undefined);
});

test('test_order_created_after_service_recovers', async () => {
  // The broker holds OrderRequested until this process consumes it.
  // Handling the queued body is the recovery path; stop/start stays manual.
  const collection = createCollection();
  const harness = createHarness(collection);
  const payload = orderRequested({
    payment_reference: 'pay_test_2',
    checkout_id: 'chk_test_2',
    lines: [
      {
        product_id: 'prod_001',
        quantity: 1,
        price_snapshot_minor_units: 1000,
      },
    ],
    total_minor_units: 1000,
  });

  assert.equal(
    await collection.findOne({ payment_reference: 'pay_test_2' }),
    null,
  );

  await handleOrderRequested(
    harness.channel,
    rabbitMessage(payload),
    harness.deps,
  );

  const order = await collection.findOne({ payment_reference: 'pay_test_2' });
  assert.ok(order);
  assert.equal(order.payment_reference, 'pay_test_2');
  assert.equal(order.total_minor_units, 1000);
});

test('test_duplicate_message_creates_one_order', async () => {
  const collection = createCollection();
  const harness = createHarness(collection);
  const payload = orderRequested({
    payment_reference: 'pay_dup',
    checkout_id: 'chk_dup',
    lines: [
      {
        product_id: 'prod_001',
        quantity: 1,
        price_snapshot_minor_units: 1000,
      },
    ],
    total_minor_units: 1000,
  });
  const first = rabbitMessage(payload);
  const second = rabbitMessage(payload);

  await handleOrderRequested(harness.channel, first, harness.deps);
  await handleOrderRequested(harness.channel, second, harness.deps);

  const orders = collection.orders.filter(
    (doc) => doc.payment_reference === 'pay_dup',
  );
  assert.equal(orders.length, 1);
  assert.equal(harness.acks.length, 2);
  assert.equal(harness.published.length, 2);
  assert.equal(
    harness.published[0].payload.order_id,
    harness.published[1].payload.order_id,
  );
});

test('test_duplicate_key_error_handled', async () => {
  const dbSource = fs.readFileSync(path.join(SERVICE_ROOT, 'db.js'), 'utf8');
  assert.match(
    dbSource,
    /createIndex\(\{ payment_reference: 1 \}, \{ unique: true \}\)/,
  );

  const existingOrderId = new ObjectId('507f1f77bcf86cd799439011');
  const existing = {
    _id: existingOrderId,
    payment_reference: 'pay_preexist',
    email: 'existing@example.com',
    ship_to: {
      street: '456 Elm St',
      city: 'Shelbyville',
      postal_code: '67890',
      country: 'US',
    },
    lines: [
      {
        product_id: 'prod_002',
        quantity: 1,
        price_minor_units: 2000,
      },
    ],
    total_minor_units: 2000,
    created_at: new Date(),
  };

  let lookups = 0;
  const collection = {
    async findOne() {
      lookups += 1;
      if (lookups === 1) {
        return null;
      }
      return existing;
    },
    async insertOne() {
      const err = new Error('E11000 duplicate key');
      err.code = 11000;
      throw err;
    },
  };
  const harness = createHarness(collection);
  const payload = orderRequested({
    payment_reference: 'pay_preexist',
    checkout_id: 'chk_preexist',
  });

  await handleOrderRequested(
    harness.channel,
    rabbitMessage(payload),
    harness.deps,
  );

  assert.equal(existing.email, 'existing@example.com');
  assert.equal(harness.acks.length, 1);
  assert.equal(harness.nacks.length, 0);
  assert.equal(harness.published.length, 1);
  assert.equal(
    harness.published[0].payload.order_id,
    existingOrderId.toString(),
  );
  assert.equal(harness.published[0].payload.payment_reference, 'pay_preexist');
  assert.equal(harness.published[0].payload.checkout_id, 'chk_preexist');
});

test('test_order_created_published_after_insert', async () => {
  const collection = createCollection();
  const harness = createHarness(collection);
  const payload = orderRequested({
    payment_reference: 'pay_publish',
    checkout_id: 'chk_publish',
    lines: [
      {
        product_id: 'prod_001',
        quantity: 1,
        price_snapshot_minor_units: 1000,
      },
    ],
    total_minor_units: 1000,
  });

  await handleOrderRequested(
    harness.channel,
    rabbitMessage(payload),
    harness.deps,
  );

  assert.equal(harness.published.length, 1);
  const orderCreated = harness.published[0].payload;
  assert.equal(orderCreated.event_type, 'OrderCreated');
  assert.equal(orderCreated.payment_reference, 'pay_publish');
  assert.equal(orderCreated.checkout_id, 'chk_publish');
  assert.match(orderCreated.order_id, /^[0-9a-f]{24}$/);
  assert.ok(orderCreated.timestamp);

  const order = collection.orders.find(
    (doc) => doc._id.toString() === orderCreated.order_id,
  );
  assert.ok(order);
  assert.equal(order.payment_reference, 'pay_publish');
});

test('test_database_isolation_order_side', () => {
  const otherDatabases = [
    'checkout_db',
    'payment_db',
    'cart_db',
    'identity_db',
    'catalog_db',
  ];
  const orderFiles = fs
    .readdirSync(SERVICE_ROOT)
    .filter((file) => file.endsWith('.js'));

  for (const file of orderFiles) {
    const source = fs.readFileSync(path.join(SERVICE_ROOT, file), 'utf8');
    for (const dbName of otherDatabases) {
      assert.equal(source.includes(dbName), false, `${dbName} found in ${file}`);
    }
  }

  const dbSource = fs.readFileSync(path.join(SERVICE_ROOT, 'db.js'), 'utf8');
  assert.equal(dbSource.includes('order_db'), true);
  assert.equal(dbSource.includes("collection('orders')"), true);
});

test('invalid OrderRequested is nacked without requeue', async () => {
  const collection = createCollection();
  const harness = createHarness(collection);
  const rabbitMsg = { content: Buffer.from('not-json') };

  await handleOrderRequested(harness.channel, rabbitMsg, harness.deps);

  assert.equal(collection.orders.length, 0);
  assert.equal(harness.acks.length, 0);
  assert.equal(harness.nacks.length, 1);
  assert.equal(harness.nacks[0].requeue, false);

  const invalid = rabbitMessage(
    orderRequested({ payment_reference: '', total_minor_units: -1 }),
  );
  await handleOrderRequested(harness.channel, invalid, harness.deps);
  assert.equal(harness.nacks.length, 2);
  assert.equal(harness.nacks[1].requeue, false);
  assert.equal(collection.orders.length, 0);
});

test('database error is nacked and requeued', async () => {
  const collection = {
    async findOne() {
      throw new Error('mongo down');
    },
    async insertOne() {
      throw new Error('insert should not run');
    },
  };
  const harness = createHarness(collection);

  await handleOrderRequested(
    harness.channel,
    rabbitMessage(orderRequested()),
    harness.deps,
  );

  assert.equal(harness.acks.length, 0);
  assert.equal(harness.nacks.length, 1);
  assert.equal(harness.nacks[0].requeue, true);
  assert.equal(harness.published.length, 0);
});

test('publish failure does not ack OrderRequested', async () => {
  const collection = createCollection();
  const harness = createHarness(collection);
  harness.deps.publishMessage = async () => {
    throw new Error('broker down');
  };

  await handleOrderRequested(
    harness.channel,
    rabbitMessage(orderRequested({ payment_reference: 'pay_nack' })),
    harness.deps,
  );

  assert.equal(collection.orders.length, 1);
  assert.equal(harness.acks.length, 0);
  assert.equal(harness.nacks.length, 1);
  assert.equal(harness.nacks[0].requeue, true);
});

test('signed-in OrderRequested stores shopper_id and omits email', async () => {
  const collection = createCollection();
  const harness = createHarness(collection);
  const payload = orderRequested({
    payment_reference: 'pay_shopper',
    shopper_id: '507f1f77bcf86cd799439011',
  });
  delete payload.email;

  await handleOrderRequested(
    harness.channel,
    rabbitMessage(payload),
    harness.deps,
  );

  assert.equal(collection.orders.length, 1);
  assert.equal(collection.orders[0].shopper_id, '507f1f77bcf86cd799439011');
  assert.equal(collection.orders[0].email, undefined);
});

test('publishMessage sets persistent delivery and waits for confirms', async () => {
  let confirmed = false;
  const channel = {
    publish(exchange, routingKey, body, options) {
      this.last = {
        exchange,
        routingKey,
        body: JSON.parse(body.toString()),
        options,
      };
      return true;
    },
    async waitForConfirms() {
      confirmed = true;
    },
  };

  await publishMessage(channel, 'ecom.events', 'order.created', {
    event_type: 'OrderCreated',
  });

  assert.equal(confirmed, true);
  assert.equal(channel.last.exchange, 'ecom.events');
  assert.equal(channel.last.routingKey, 'order.created');
  assert.equal(channel.last.options.persistent, true);
  assert.equal(channel.last.body.event_type, 'OrderCreated');
});

test('setupExchangeAndQueue declares a durable topic binding', async () => {
  const calls = [];
  const channel = {
    async assertExchange(...args) {
      calls.push(['exchange', ...args]);
    },
    async assertQueue(...args) {
      calls.push(['queue', ...args]);
    },
    async bindQueue(...args) {
      calls.push(['bind', ...args]);
    },
  };

  await setupExchangeAndQueue(
    channel,
    'ecom.events',
    'order.requested.queue',
    'order.requested',
  );

  assert.deepEqual(calls, [
    ['exchange', 'ecom.events', 'topic', { durable: true }],
    ['queue', 'order.requested.queue', { durable: true }],
    ['bind', 'order.requested.queue', 'ecom.events', 'order.requested'],
  ]);
});

test('consumeOrderRequested declares the order queues and consumes manually', async () => {
  const amqp = require('amqplib');
  const calls = {
    exchanges: [],
    queues: [],
    binds: [],
    prefetch: null,
    consume: null,
    uri: null,
  };
  const channel = {
    async assertExchange(...args) {
      calls.exchanges.push(args);
    },
    async assertQueue(...args) {
      calls.queues.push(args);
    },
    async bindQueue(...args) {
      calls.binds.push(args);
    },
    async prefetch(count) {
      calls.prefetch = count;
    },
    async consume(queue, handler, options) {
      calls.consume = { queue, handler, options };
    },
    on() {},
    async close() {},
  };
  const connection = {
    async createConfirmChannel() {
      return channel;
    },
    on() {},
    async close() {},
  };
  const originalConnect = amqp.connect;
  amqp.connect = async (uri) => {
    calls.uri = uri;
    return connection;
  };

  try {
    await consumeOrderRequested();
  } finally {
    amqp.connect = originalConnect;
    await closeRabbitMQ();
  }

  assert.equal(calls.uri, process.env.RABBITMQ_URI || 'amqp://localhost:5672');
  assert.equal(calls.exchanges[0][0], 'ecom.events');
  assert.equal(calls.exchanges[0][1], 'topic');
  assert.equal(calls.exchanges[0][2].durable, true);
  assert.deepEqual(
    calls.queues.map((args) => args[0]),
    ['order.requested.queue', 'order.created.queue'],
  );
  assert.deepEqual(
    calls.binds.map((args) => args[2]),
    ['order.requested', 'order.created'],
  );
  assert.equal(calls.prefetch, 10);
  assert.equal(calls.consume.queue, 'order.requested.queue');
  assert.equal(calls.consume.options.noAck, false);
  assert.equal(typeof calls.consume.handler, 'function');
});
