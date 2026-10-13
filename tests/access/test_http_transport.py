"""Real loopback Uvicorn listener on an ephemeral socket; factory owns its thread."""

import socket
import threading
import time

import httpx
import uvicorn
from access_support import PASSWORD, configured_hash
from ai_agent_adapters.configuration import BootstrapSettings

from ai_agent_core.bootstrap import create_app


def test_real_loopback_login_read_stop_and_foreign_host():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    origin = "http://127.0.0.1:" + str(port)
    settings = BootstrapSettings(
        memory_scope="test",
        core_host_port=port,
        operator_password_hash=configured_hash(),
        operator_origin=origin,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            lambda: create_app(settings),
            factory=True,
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_level="warning",
            ws="websockets",
            ws_per_message_deflate=False,
            ws_max_size=4096,
            ws_max_queue=1,
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(base_url=origin, timeout=5) as client:
            assert client.get("/api/v1/auth/session").status_code == 401
            login = client.post(
                "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": origin}
            )
            assert login.status_code == 200, login.text
            query = login.json()["scopes"][0]
            query = {
                name: query[name]
                for name in ("workspace_id", "persona_id", "memory_space_id", "memory_mode")
            }
            read = client.get("/api/v1/sessions", params=query)
            assert read.status_code == 200
            run = read.json()["items"][0]
            assert run["state"] == "STOPPED"
            stop = client.post(
                "/api/v1/agents/primary/stop",
                json={
                    "type": "agent.stop",
                    "request_id": "transport-stop",
                    "scope": run["scope"],
                    "target": "primary",
                },
                headers={"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]},
            )
            assert stop.status_code == 200 and stop.json()["state"] == "STOPPED"
            assert (
                client.get("/api/v1/auth/session", headers={"Host": "evil.invalid"}).status_code
                == 403
            )
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()
    assert not thread.is_alive()


def test_real_websocket_handshake_authorization_stop_and_frame_bound():
    import json

    import pytest
    from websockets.exceptions import ConnectionClosedError, InvalidStatus
    from websockets.sync.client import connect

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    origin = "http://127.0.0.1:" + str(port)
    uri = "ws://127.0.0.1:" + str(port) + "/api/v1/events"
    settings = BootstrapSettings(
        memory_scope="test",
        core_host_port=port,
        operator_origin=origin,
        operator_password_hash=configured_hash(),
    )
    server = uvicorn.Server(
        uvicorn.Config(
            lambda: create_app(settings),
            factory=True,
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_level="warning",
            ws="websockets",
            ws_max_size=4096,
            ws_max_queue=1,
            ws_per_message_deflate=False,
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(base_url=origin, timeout=5) as client:
            login = client.post(
                "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": origin}
            )
            assert login.status_code == 200
            cookie = "ai_operator_session=" + client.cookies.get("ai_operator_session")
            for rejected_origin, rejected_cookie in (
                (None, cookie),
                ("https://evil.invalid", cookie),
                (origin, "ai_operator_session=" + "x" * 43),
            ):
                with pytest.raises(InvalidStatus) as failure:
                    connect(
                        uri,
                        origin=rejected_origin,
                        additional_headers={"Cookie": rejected_cookie},
                        open_timeout=2,
                        proxy=None,
                    )
                assert failure.value.response.status_code == 403
            query = login.json()["scopes"][0]
            query = {
                name: query[name]
                for name in ("workspace_id", "persona_id", "memory_space_id", "memory_mode")
            }
            run = client.get("/api/v1/sessions", params=query).json()["items"][0]
            with connect(
                uri,
                origin=origin,
                additional_headers={"Cookie": cookie},
                open_timeout=2,
                close_timeout=1,
                proxy=None,
            ) as ws:
                ws.send(json.dumps({"scope": run["scope"]}))
                time.sleep(0.05)
                response = client.post(
                    "/api/v1/agents/primary/stop",
                    json={
                        "type": "agent.stop",
                        "request_id": "real-ws-stop",
                        "scope": run["scope"],
                        "target": "primary",
                    },
                    headers={"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]},
                )
                assert response.status_code == 200
                received = json.loads(ws.recv(timeout=2))
                assert received["type"] == "run.changed"
                assert received["scope"] == run["scope"]
                ws.send("x" * 4097)
                with pytest.raises(ConnectionClosedError) as closed:
                    ws.recv(timeout=2)
                assert closed.value.rcvd.code == 1009
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()
    assert not thread.is_alive()
