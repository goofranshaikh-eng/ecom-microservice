const Joi = require('joi');

// Amounts are integers in minor units. No floating-point fields. [AD-5]
// [CURRENCY_DEFERRED] single implicit currency; no currency code.
// [TOTAL_VALIDATION_DEFERRED] total_minor_units is trusted from checkout.
// [ADDRESS_VALIDATION_DEFERRED] ship_to is stored as given.
// [ORDER_DISPLAY_DEFERRED] lines store product_id only.
// [TAX_SHIPPING_DEFERRED] total_minor_units is the full charge.
// [ORDER_STATUS_DEFERRED] no status field; a stored order is paid.

const shipToSchema = Joi.object({
  street: Joi.string().trim().min(1).required(),
  city: Joi.string().trim().min(1).required(),
  postal_code: Joi.string().trim().min(1).required(),
  country: Joi.string().trim().min(1).required(),
});

const orderLineSchema = Joi.object({
  product_id: Joi.string().trim().min(1).required(),
  quantity: Joi.number().integer().positive().required(),
  price_minor_units: Joi.number().integer().min(0).required(),
});

const createOrderSchema = Joi.object({
  shopper_id: Joi.string().trim().min(1),
  email: Joi.string().trim().min(1),
  ship_to: shipToSchema.required(),
  lines: Joi.array().items(orderLineSchema).min(1).required(),
  total_minor_units: Joi.number().integer().min(0).required(),
  payment_reference: Joi.string().trim().min(1).required(),
});

module.exports = {
  shipToSchema,
  orderLineSchema,
  createOrderSchema,
};
