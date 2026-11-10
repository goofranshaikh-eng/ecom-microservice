import os

import uvicorn
from fastapi import FastAPI

from routes import router

app = FastAPI(
    title="Identity Service",
    version="1.0.0",
    docs_url="/docs",
    description=(
        "Identity owns shopper sign-in and shopper lookup. "
        "All endpoints are public in v1; there is no role-based access control."
    ),
)
app.include_router(router)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8001"))
    uvicorn.run(app, host="0.0.0.0", port=port)
