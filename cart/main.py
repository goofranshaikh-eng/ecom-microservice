import os

import uvicorn
from fastapi import FastAPI

from routes import router

app = FastAPI(
    title="Cart Service",
    version="1.0.0",
    docs_url="/docs",
    description=(
        "Cart owns cart lines and totals. Prices are snapshotted from catalog at add time. "
        "All endpoints are public in v1; there is no authentication. [AUTH_DEFERRED]"
    ),
)
app.include_router(router)

if __name__ == "__main__":
    # Cart listens on 8003 unless PORT is set. [CONFIG_DEFERRED]
    port = int(os.environ.get("PORT", "8003"))
    uvicorn.run(app, host="0.0.0.0", port=port)
