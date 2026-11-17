import os

import uvicorn
from fastapi import FastAPI

import db
from routes import router

app = FastAPI(
    title="Checkout Service",
    version="1.0.0",
    docs_url="/docs",
    description=(
        "Checkout orchestrates cart, payment, and order over HTTP. "
        "Public, no auth. [AUTH_DEFERRED]"
    ),
)
# checkout_db handle. v1 writes nothing; future sessions live here.
app.state.db = db.db
app.include_router(router)

if __name__ == "__main__":
    # Checkout listens on 8004 unless PORT is set. [CONFIG_DEFERRED]
    port = int(os.environ.get("PORT", "8004"))
    uvicorn.run(app, host="0.0.0.0", port=port)
