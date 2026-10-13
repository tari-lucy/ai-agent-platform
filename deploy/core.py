"""M0 container composition: static panel plus empty core, no agent runtime."""

import os

import uvicorn
from starlette.staticfiles import StaticFiles

from ai_agent_core.bootstrap import create_app

app = create_app()
app.mount("/", StaticFiles(directory="/opt/platform/panel", html=True), name="panel")

if __name__ == "__main__":
    print("Core scaffold ready; agent runtime is not started.", flush=True)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("CORE_CONTAINER_PORT", "8080")),
        workers=1,
        access_log=False,
        log_level="warning",
    )
