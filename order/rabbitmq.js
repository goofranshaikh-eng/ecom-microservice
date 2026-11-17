const amqp = require('amqplib');

// [CONFIG_DEFERRED] RABBITMQ_URI comes from the environment; no .env loading.
// [NO_RETRY] connection failure throws. The consumer exits the process.
const RABBITMQ_URI = process.env.RABBITMQ_URI || 'amqp://localhost:5672';
const EXCHANGE_NAME = 'ecom.events';

let connection = null;
let channel = null;

async function connectRabbitMQ() {
  try {
    connection = await amqp.connect(RABBITMQ_URI);
    // Confirm channel so publishMessage can wait for broker acks.
    channel = await connection.createConfirmChannel();
    return { connection, channel };
  } catch (err) {
    console.error('Failed to connect to RabbitMQ:', err);
    throw err;
  }
}

async function setupExchangeAndQueue(ch, exchange, queue, routingKey) {
  await ch.assertExchange(exchange, 'topic', { durable: true });
  await ch.assertQueue(queue, { durable: true });
  await ch.bindQueue(queue, exchange, routingKey);
}

async function publishMessage(ch, exchange, routingKey, payload) {
  // persistent: true sets delivery_mode=2.
  ch.publish(exchange, routingKey, Buffer.from(JSON.stringify(payload)), {
    persistent: true,
  });
  await ch.waitForConfirms();
}

async function closeRabbitMQ() {
  try {
    if (channel) {
      const openChannel = channel;
      channel = null;
      await openChannel.close();
    }
    if (connection) {
      const openConnection = connection;
      connection = null;
      await openConnection.close();
    }
    console.log('RabbitMQ connection closed');
  } catch (err) {
    console.error('Error closing RabbitMQ:', err);
  }
}

module.exports = {
  RABBITMQ_URI,
  EXCHANGE_NAME,
  connectRabbitMQ,
  setupExchangeAndQueue,
  publishMessage,
  closeRabbitMQ,
};
