"""M0 container composition: static panel plus empty core, no agent runtime."""

import sys

import uvicorn
from ai_agent_adapters.configuration import ConfigurationError
from starlette.staticfiles import StaticFiles

from ai_agent_core.bootstrap import create_app

try:
    app = create_app()
except ConfigurationError as error:
    print(str(error), file=sys.stderr)
    raise SystemExit(78) from None
app.mount("/", StaticFiles(directory="/opt/platform/panel", html=True), name="panel")

if __name__ == "__main__":
    print("Core scaffold ready; agent runtime is not started.", flush=True)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(app.state.bootstrap_manifest["core_container_port"]),
        workers=1,
        access_log=False,
        log_level="warning",
        ws="websockets",
        ws_per_message_deflate=False,
        ws_max_size=4096,
        ws_max_queue=1,
        backlog=128,
        timeout_keep_alive=5,
    )
