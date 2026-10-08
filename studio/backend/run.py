"""QuicStudio data backend entry point."""

import uvicorn

import data.bootstrap  # noqa: F401
from data.config import settings

if __name__ == "__main__":
    uvicorn.run("data.main:app", host=settings.api_host, port=settings.api_port, reload=True)
