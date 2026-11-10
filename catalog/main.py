import os

import uvicorn
from fastapi import FastAPI

from routes import router

app = FastAPI(
    title="Catalog Service",
    version="1.0.0",
    docs_url="/docs",
    description=(
        "Catalog owns product names, prices, and whether a product can be added to a cart. "
        "All endpoints are public in v1; there is no authentication. [AUTH_DEFERRED]"
    ),
)
app.include_router(router)

if __name__ == "__main__":
    # Catalog listens on 8002 unless PORT is set. [CONFIG_DEFERRED]
    port = int(os.environ.get("PORT", "8002"))
    uvicorn.run(app, host="0.0.0.0", port=port)
