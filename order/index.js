const path = require('path');

const express = require('express');
const swaggerUi = require('swagger-ui-express');
const swaggerJsdoc = require('swagger-jsdoc');

const { connectDB } = require('./db');
const routes = require('./routes');

// [AUTH_DEFERRED] every endpoint is public.
// [HEALTH_CHECK_DEFERRED] no /health endpoint.
// [LOGGING_DEFERRED] console logging only.
// [CONFIG_DEFERRED] PORT and MONGODB_URI come from the environment; no .env loading.
// [GRACEFUL_SHUTDOWN_DEFERRED] no SIGTERM handler.
// [TESTS_EXTERNAL] integration tests run outside this package.
// [VERSION_CURRENT] dependency pins live in package.json.
// Order does not call any other service. [AD-2]

const app = express();
app.use(express.json());

const port = process.env.PORT || 8006;

const swaggerOptions = {
  definition: {
    openapi: '3.0.0',
    info: {
      title: 'Order Service',
      version: '1.0.0',
      description:
        'Order creation and retrieval API. Public, no auth. [AUTH_DEFERRED] [ORDER_OWNERSHIP_DEFERRED]',
    },
    servers: [{ url: `http://localhost:${port}` }],
  },
  apis: [path.join(__dirname, 'routes.js')],
};

const swaggerSpec = swaggerJsdoc(swaggerOptions);
app.use(
  '/docs',
  swaggerUi.serve,
  swaggerUi.setup(swaggerSpec, { customSiteTitle: 'Order Service' }),
);

app.use('/orders', routes);

// Order listens on 8006 unless PORT is set.
connectDB()
  .then(() => {
    app.listen(port, () => {
      console.log(`Order service running on port ${port}`);
      console.log(`Swagger UI: http://localhost:${port}/docs`);
    });
  })
  .catch((err) => {
    // [NO_RETRY] connection failure exits the process.
    console.error('Failed to connect to database:', err);
    process.exit(1);
  });
