"""Process entry point for the Hark2API server."""
from __future__ import annotations

from api.app import app
from config import CFG


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=CFG.host, port=CFG.port)
