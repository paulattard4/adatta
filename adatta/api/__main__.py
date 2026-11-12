"""uv run python -m adatta.api: serve the dashboard API on 127.0.0.1 only."""

import uvicorn

from adatta.api.app import HOST, PORT, production_app

if __name__ == "__main__":
    uvicorn.run(production_app(), host=HOST, port=PORT)
