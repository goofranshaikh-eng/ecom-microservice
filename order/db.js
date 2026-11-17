const { MongoClient } = require('mongodb');

// Order documents live only in order_db.orders. This service never opens
// another database and never calls another service.
//   _id: ObjectId (order_id)
//   shopper_id: string (optional)
//   email: string (optional; required for a guest order)
//   ship_to: { street, city, postal_code, country }
//   lines: [{ product_id, quantity, price_minor_units }]
//   total_minor_units: integer (>= 0)
//   payment_reference: string
//   created_at: ISODate
// [CONFIG_DEFERRED] URI comes from the environment; no .env loading.
// [NO_RETRY] fail fast on connection errors. No pooling overrides.
// [OPTIONAL_INDEXES] shopper_id, email, and created_at are created on connect.
// [IDEMPOTENCY_DEFERRED] no unique index for a duplicate create.
// [ORDER_ARCHIVAL_DEFERRED] no TTL index.
// [AUDIT_LOG_DEFERRED] no audit_log collection.
// [GRACEFUL_SHUTDOWN_DEFERRED] the client is not closed on SIGTERM.

const uri = process.env.MONGODB_URI || 'mongodb://localhost:27017';
const client = new MongoClient(uri);

let ordersCollection;

async function connectDB() {
  await client.connect();
  const db = client.db('order_db');
  ordersCollection = db.collection('orders');

  // [OPTIONAL_INDEXES]
  await ordersCollection.createIndex({ shopper_id: 1 });
  await ordersCollection.createIndex({ email: 1 });
  await ordersCollection.createIndex({ created_at: -1 });
}

module.exports = {
  connectDB,
  getOrdersCollection: () => ordersCollection,
};
