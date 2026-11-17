const express = require('express');
const { ObjectId } = require('mongodb');

const { createOrderSchema } = require('./models');
const { getOrdersCollection } = require('./db');

// Public, no auth. Any client can create or retrieve any order. [AUTH_DEFERRED]
// [ORDER_OWNERSHIP_DEFERRED] GET does not check that the caller owns the order.
// [IDEMPOTENCY_DEFERRED] a repeated POST /orders creates another order.
// [PAGINATION_DEFERRED] no list endpoint.
// [ORDER_UPDATES_DEFERRED] no PATCH.
// [ORDER_CANCELLATION_DEFERRED] no DELETE or cancel.
// [ORDER_SEARCH_DEFERRED] no search endpoint.
// [ORDER_HISTORY_DEFERRED] no shopper order history endpoint.
// [EMAIL_DEFERRED] no confirmation email.
// [LOGGING_DEFERRED] console.error only in v1.
// [ERROR_DETAIL_DEFERRED] database failures return a fixed message.
// This module does not call catalog, cart, checkout, payment, or identity.

const router = express.Router();

/**
 * @swagger
 * components:
 *   schemas:
 *     ShipTo:
 *       type: object
 *       required: [street, city, postal_code, country]
 *       properties:
 *         street:
 *           type: string
 *         city:
 *           type: string
 *         postal_code:
 *           type: string
 *         country:
 *           type: string
 *     OrderLine:
 *       type: object
 *       required: [product_id, quantity, price_minor_units]
 *       properties:
 *         product_id:
 *           type: string
 *         quantity:
 *           type: integer
 *           minimum: 1
 *         price_minor_units:
 *           type: integer
 *           minimum: 0
 *     CreateOrderRequest:
 *       type: object
 *       required: [ship_to, lines, total_minor_units, payment_reference]
 *       description: At least one of shopper_id or email is required. Public, no auth. [AUTH_DEFERRED]
 *       properties:
 *         shopper_id:
 *           type: string
 *           description: Shopper id from identity. Optional when email is set.
 *         email:
 *           type: string
 *           description: Contact email. Optional when shopper_id is set. Required for a guest order.
 *         ship_to:
 *           $ref: '#/components/schemas/ShipTo'
 *         lines:
 *           type: array
 *           minItems: 1
 *           items:
 *             $ref: '#/components/schemas/OrderLine'
 *         total_minor_units:
 *           type: integer
 *           minimum: 0
 *         payment_reference:
 *           type: string
 *     OrderResponse:
 *       type: object
 *       required: [order_id, ship_to, lines, total_minor_units, payment_reference, created_at]
 *       properties:
 *         order_id:
 *           type: string
 *         shopper_id:
 *           type: string
 *         email:
 *           type: string
 *         ship_to:
 *           $ref: '#/components/schemas/ShipTo'
 *         lines:
 *           type: array
 *           items:
 *             $ref: '#/components/schemas/OrderLine'
 *         total_minor_units:
 *           type: integer
 *         payment_reference:
 *           type: string
 *         created_at:
 *           type: string
 *           format: date-time
 *           description: ISO 8601 timestamp
 *     ErrorResponse:
 *       type: object
 *       required: [error]
 *       properties:
 *         error:
 *           type: string
 */

/**
 * @swagger
 * /orders:
 *   post:
 *     summary: Create a new order
 *     description: >
 *       Public, no auth. [AUTH_DEFERRED] Stores the paid order sent by checkout.
 *       Either shopper_id or email is required. [IDEMPOTENCY_DEFERRED]
 *       [TOTAL_VALIDATION_DEFERRED] [EMAIL_DEFERRED]
 *     requestBody:
 *       required: true
 *       content:
 *         application/json:
 *           schema:
 *             $ref: '#/components/schemas/CreateOrderRequest'
 *           example:
 *             email: guest@example.com
 *             ship_to:
 *               street: 123 Main St
 *               city: Springfield
 *               postal_code: "12345"
 *               country: USA
 *             lines:
 *               - product_id: prod1
 *                 quantity: 2
 *                 price_minor_units: 1000
 *             total_minor_units: 2000
 *             payment_reference: pay_abc123
 *     responses:
 *       201:
 *         description: Order created
 *         content:
 *           application/json:
 *             schema:
 *               type: object
 *               required: [order_id]
 *               properties:
 *                 order_id:
 *                   type: string
 *       400:
 *         description: Validation failed, or both shopper_id and email are missing
 *         content:
 *           application/json:
 *             schema:
 *               $ref: '#/components/schemas/ErrorResponse'
 *       500:
 *         description: Database insertion failure
 *         content:
 *           application/json:
 *             schema:
 *               $ref: '#/components/schemas/ErrorResponse'
 *             example:
 *               error: Failed to create order
 */
router.post('/', async (req, res) => {
  const { error, value } = createOrderSchema.validate(req.body, {
    abortEarly: false,
  });
  if (error) {
    return res.status(400).json({ error: error.details[0].message });
  }

  // Guest orders carry email; signed-in orders carry shopper_id. [AD-6]
  if (!value.shopper_id && !value.email) {
    return res.status(400).json({
      error: 'Either shopper_id or email is required',
    });
  }

  const orderDoc = {
    ...(value.shopper_id ? { shopper_id: value.shopper_id } : {}),
    ...(value.email ? { email: value.email } : {}),
    ship_to: value.ship_to,
    lines: value.lines,
    total_minor_units: value.total_minor_units,
    payment_reference: value.payment_reference,
    created_at: new Date(),
  };

  try {
    const result = await getOrdersCollection().insertOne(orderDoc);
    return res.status(201).json({ order_id: result.insertedId.toString() });
  } catch (err) {
    // [ERROR_DETAIL_DEFERRED] fixed message; the response has no stack trace.
    console.error('[ERROR_DETAIL_DEFERRED] Failed to create order:', err);
    return res.status(500).json({ error: 'Failed to create order' });
  }
});

/**
 * @swagger
 * /orders/{order_id}:
 *   get:
 *     summary: Retrieve a stored order
 *     description: >
 *       Public, no auth. [AUTH_DEFERRED] [ORDER_OWNERSHIP_DEFERRED]
 *       Returns the order id, lines, total, payment reference, and ship-to address.
 *     parameters:
 *       - in: path
 *         name: order_id
 *         required: true
 *         schema:
 *           type: string
 *         description: Order id (MongoDB ObjectId hex string)
 *     responses:
 *       200:
 *         description: Stored order
 *         content:
 *           application/json:
 *             schema:
 *               $ref: '#/components/schemas/OrderResponse'
 *       400:
 *         description: order_id is not a valid ObjectId
 *         content:
 *           application/json:
 *             schema:
 *               $ref: '#/components/schemas/ErrorResponse'
 *             example:
 *               error: Invalid order_id format
 *       404:
 *         description: Order not found
 *         content:
 *           application/json:
 *             schema:
 *               $ref: '#/components/schemas/ErrorResponse'
 *             example:
 *               error: Order not found
 *       500:
 *         description: Database query failure
 *         content:
 *           application/json:
 *             schema:
 *               $ref: '#/components/schemas/ErrorResponse'
 *             example:
 *               error: Database query failed
 */
router.get('/:order_id', async (req, res) => {
  let oid;
  try {
    oid = new ObjectId(req.params.order_id);
  } catch (err) {
    return res.status(400).json({ error: 'Invalid order_id format' });
  }

  try {
    const doc = await getOrdersCollection().findOne({ _id: oid });
    if (!doc) {
      return res.status(404).json({ error: 'Order not found' });
    }

    const response = {
      order_id: doc._id.toString(),
      ...(doc.shopper_id !== undefined ? { shopper_id: doc.shopper_id } : {}),
      ...(doc.email !== undefined ? { email: doc.email } : {}),
      ship_to: doc.ship_to,
      lines: doc.lines,
      total_minor_units: doc.total_minor_units,
      payment_reference: doc.payment_reference,
      created_at: doc.created_at.toISOString(),
    };
    return res.status(200).json(response);
  } catch (err) {
    // [ERROR_DETAIL_DEFERRED] fixed message; the response has no stack trace.
    console.error('[ERROR_DETAIL_DEFERRED] Database query failed:', err);
    return res.status(500).json({ error: 'Database query failed' });
  }
});

module.exports = router;
