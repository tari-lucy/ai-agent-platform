"""One real end-to-end run on native Linux: HTTP over loopback to a separate `serve` process.

login -> STOPPED -> explicit start -> private mock dialogue -> delivery acknowledgment ->
TEST laboratory and persisted report -> STOP -> restart is STOPPED again. Mock provider only;
no external network, no live API. Synthetic profile and generated local credentials.
"""

import json
import sys

import pytest
from host_support import Server, free_port, init, scope_query, wait_for

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="native Linux evidence")


def test_end_to_end_mock_host(tmp_path):
    root, log = tmp_path / "host", tmp_path / "serve.log"
    done = init(root, "--port", "1024")
    assert done.returncode == 0, done.stderr
    password = (root / "secrets" / "operator-password.txt").read_text().strip()
    config = json.loads((root / "host.json").read_text())
    port = free_port()
    steps: list[str] = []

    with Server(root, port, log) as server, server.client() as http:
        origin = {"Origin": server.origin}
        assert http.get("/api/v1/auth/session").status_code == 401
        refused = http.post("/api/v1/auth/login", json={"password": "wrong"}, headers=origin)
        assert refused.status_code == 403 and "csrf_token" not in refused.text
        login = http.post("/api/v1/auth/login", json={"password": password}, headers=origin)
        assert login.status_code == 200, login.text
        headers = {**origin, "X-CSRF-Token": login.json()["csrf_token"]}
        steps.append("login")

        def work_scope():
            return http.get("/api/v1/auth/session").json()

        def sessions(query):
            response = http.get("/api/v1/sessions", params=query)
            assert response.status_code == 200, response.text
            return response.json()["items"][0]

        base = {
            "workspace_id": config["workspace_id"],
            "persona_id": config["persona_id"],
            "memory_space_id": config["work_space_id"],
            "memory_mode": "work",
        }
        run = sessions(base)
        assert run["state"] == "STOPPED"
        steps.append("STOPPED")

        def post(body):
            return http.post("/api/v1/commands", json=body, headers=headers)

        started = post(
            dict(
                type="agent.start",
                request_id="smoke-start",
                target="primary",
                scope=run["scope"],
                expected_version=run["version"],
                arguments={"autonomy": "autonomous"},
            )
        )
        assert started.status_code == 200, started.text
        run = sessions(base)
        assert run["state"] == "ACTIVE"
        steps.append("start")

        message = post(
            dict(
                type="dialogue.message",
                request_id="smoke-message",
                target=run["session_id"],
                scope=run["scope"],
                arguments={"text": "Вымышленное приветствие."},
            )
        )
        assert message.status_code == 200, message.text
        utterances = wait_for(
            lambda: http.get("/api/v1/utterances", params=base).json()["items"],
            bool,
            "private final",
        )
        reply = utterances[0]
        assert reply["moderation_status"] == "draft" and reply["content"]
        steps.append("final")
        ack = post(
            dict(
                type="dialogue.acknowledge",
                request_id="smoke-ack",
                target=reply["id"],
                scope=run["scope"],
                expected_version=reply["version"],
                arguments={"revision": reply["revision"]},
            )
        )
        assert ack.status_code == 200, ack.text
        deliveries = http.get("/api/v1/deliveries", params=base).json()["items"]
        assert [d["status"] for d in deliveries] == ["DELIVERED"], deliveries
        steps.append("ack")
        attempts = http.get("/api/v1/llm-attempts", params=base).json()["items"]
        assert len(attempts) == 1 and attempts[0]["status"] == "completed"
        # The mock reports no usage and there is no tariff: unknown stays unknown, never zero.
        usage = attempts[0]["usage"]
        assert usage["source"] == "unknown" and usage["completeness"] == "unknown"
        assert usage["input_tokens"] is None and usage["output_tokens"] is None
        assert attempts[0]["tariff_id"] is None

        catalogue = http.get("/api/v1/laboratory-catalogue", params=base)
        assert catalogue.status_code == 200, catalogue.text
        scenario = catalogue.json()["scenarios"][0]
        created = post(
            dict(
                type="laboratory.create",
                request_id="smoke-lab-create",
                target="laboratory",
                scope=run["scope"],
                arguments={
                    "schema_id": "laboratory.create-args",
                    "data": {"persona_id": config["persona_id"]},
                },
            )
        )
        assert created.status_code == 200, created.text
        lab = {**base, "memory_space_id": created.json()["resource_id"], "memory_mode": "test"}
        lab_scope = dict(lab)  # the TEST partition only: no agent/run/session
        run = sessions(base)
        queued = post(
            dict(
                type="laboratory.start",
                request_id="smoke-lab-start",
                target="laboratory",
                scope=run["scope"],
                expected_version=run["version"],
                arguments={
                    "schema_id": "laboratory.start-args",
                    "data": {
                        "scenario_id": scenario["scenario_id"],
                        "scenario_version": scenario["version"],
                        "lab_scope": lab_scope,
                        "seed": "smoke-seed",
                        "mode": "mock",
                    },
                },
            )
        )
        assert queued.status_code == 200, queued.text
        reports = wait_for(
            lambda: http.get("/api/v1/laboratory-reports", params=lab).json()["items"],
            bool,
            "laboratory report",
            60,
        )
        content = http.get(f"/api/v1/laboratory-reports/{reports[0]['id']}/content", params=lab)
        assert content.status_code == 200, content.text
        assert content.json()["mock"] is True and content.json()["findings"]
        steps.append("lab-report")

        def lab_start(request, version):
            body = dict(queued.request.content and json.loads(queued.request.content))
            body.update(request_id=request, expected_version=version)
            return post(body)

        current = sessions(base)
        # A stale control version is refused, and so is a TEST scope that is already used.
        assert lab_start("smoke-lab-stale", run["version"] - 1).status_code == 409
        reused = lab_start("smoke-lab-reuse", current["version"])
        assert reused.status_code == 409 and reused.json()["code"] == "VERSION_CONFLICT"
        assert len(http.get("/api/v1/laboratory-reports", params=lab).json()["items"]) == 1
        steps.append("negatives")

        stopped = http.post(
            "/api/v1/agents/primary/stop",
            headers=headers,
            json=dict(
                type="agent.stop",
                request_id="smoke-stop",
                target="primary",
                scope=sessions(base)["scope"],
            ),
        )
        assert stopped.status_code == 200, stopped.text
        # STOP may report STOPPING until the owners confirm their resources; wait, never force.
        assert stopped.json()["state"] in ("STOPPING", "STOPPED"), stopped.text
        wait_for(lambda: sessions(base)["state"], lambda state: state == "STOPPED", "STOPPED")
        steps.append("stop")

    with Server(root, port, log) as server, server.client() as http:
        login = http.post(
            "/api/v1/auth/login", json={"password": password}, headers={"Origin": server.origin}
        )
        assert login.status_code == 200
        assert sessions_state(http, base) == "STOPPED"
        steps.append("restart-STOPPED")

    text = log.read_text(errors="replace")
    assert password not in text and "Вымышленное приветствие" not in text
    assert steps == [
        "login",
        "STOPPED",
        "start",
        "final",
        "ack",
        "lab-report",
        "negatives",
        "stop",
        "restart-STOPPED",
    ]


def sessions_state(http, base):
    return http.get("/api/v1/sessions", params=scope_query(base)).json()["items"][0]["state"]
