import os

import uvicorn
from fastapi import FastAPI

from routes import router

app = FastAPI(
    title="Payment Service",
    version="1.0.0",
    docs_url="/docs",
    description=(
        "Payment is a deterministic charge test double. "
        "All endpoints are public in v1; there is no authentication. [AUTH_DEFERRED]"
    ),
)
app.include_router(router)

if __name__ == "__main__":
    # Payment listens on 8005 unless PORT is set. [CONFIG_DEFERRED]
    port = int(os.environ.get("PORT", "8005"))
    uvicorn.run(app, host="0.0.0.0", port=port)
