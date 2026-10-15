"""Production init grants and real HTTP controls/settings transfer; no fixture grant widening."""

import asyncio
import json
from datetime import UTC, datetime

from ai_agent_application.storage import Owner
from controls_support import seed_change
from host_support import Server, free_port, init

from ai_agent_core.local_host import rights
from ai_agent_core.local_host.layout import HostConfig


def test_controls_mode_is_explicit_bounded_and_does_not_grant_operator_writes(tmp_path):
    default = rights.work_grants()
    assert not any(g.write for g in default if g.owner is Owner.WORKSPACE)
    enabled = rights.work_grants(operator_controls=True)
    assert any(
        g.write and g.principal.owner is Owner.WORKSPACE
        for g in enabled
        if g.owner is Owner.WORKSPACE
    )
    for grant in (*enabled, *rights.settings_grants()):
        assert not grant.public_disclosure and grant.owner is not Owner.MODERATION
        if grant.principal.role == "operator":
            assert not grant.write
    assert {g.owner for g in rights.settings_grants() if g.write} == {
        Owner.PERSONA,
        Owner.GM,
        Owner.WORKSPACE,
    }
    root = tmp_path / "invalid"
    result = init(root, "--operator-controls", "--test-scopes", "7")
    assert result.returncode == 78 and not root.exists()
    result = init(root, "--operator-controls", "--persona-id", "x" * 64)
    assert result.returncode == 78 and not root.exists()
    legacy = HostConfig(
        workspace_id="local",
        persona_id="persona",
        work_space_id="work",
        test_space_ids=("test",),
        port=8765,
        permission_expires_at=datetime.now(UTC).isoformat(),
    )
    assert not legacy.operator_controls and legacy.settings_space_id is None


def test_production_host_accepts_decision_rollback_and_reviewed_settings_transfer(tmp_path):
    root = tmp_path / "host"
    done = init(root, "--operator-controls", "--test-scopes", "2")
    assert done.returncode == 0, done.stderr
    change = asyncio.run(seed_change(root))
    password = (root / "secrets" / "operator-password.txt").read_text().strip()
    config = json.loads((root / "host.json").read_text())
    assert config["operator_controls"] and len(config["test_space_ids"]) == 2
    work = dict(
        workspace_id="local",
        persona_id="persona",
        memory_space_id="persona-work",
        memory_mode="work",
    )
    test = dict(work, memory_mode="test", memory_space_id=config["settings_space_id"])
    port, log = free_port(), tmp_path / "host.log"
    with Server(root, port, log) as server, server.client() as http:
        login = http.post(
            "/api/v1/auth/login", json={"password": password}, headers={"Origin": server.origin}
        )
        assert login.status_code == 200
        headers = {"Origin": server.origin, "X-CSRF-Token": login.json()["csrf_token"]}
        assert len(login.json()["scopes"]) == 4
        pool = dict(test, memory_space_id=config["test_space_ids"][0])
        bad_create = http.post(
            "/api/v1/commands",
            headers=headers,
            json=dict(
                type="laboratory.create",
                request_id="not-from-test-settings",
                target="laboratory",
                scope=pool,
                arguments=dict(schema_id="laboratory.create-args", data={"persona_id": "persona"}),
            ),
        )
        assert bad_create.status_code == 403, bad_create.text
        bad_message = http.post(
            "/api/v1/commands",
            headers=headers,
            json=dict(
                type="dialogue.message",
                request_id="not-a-test-run",
                target="anything",
                scope=test,
                arguments={"text": "Вымышленная запрещённая отправка."},
            ),
        )
        assert bad_message.status_code == 403, bad_message.text

        def read(kind, scope=work):
            response = http.get("/api/v1/" + kind, params=scope)
            assert response.status_code == 200, response.text
            return response.json()["items"]

        def post(kind, target, version, data, *, schema=None, scope=work, request=None):
            body = dict(
                type=kind,
                target=target,
                expected_version=version,
                scope=scope,
                request_id=request or kind,
                arguments=dict(schema_id=schema or kind + "-args", data=data),
            )
            return http.post("/api/v1/commands", json=body, headers=headers)

        assert read("sessions")[0]["state"] == "STOPPED"
        permissions = read("permissions")
        original = read("character-states")[0]
        accepted = post(
            "change.decision",
            change.id,
            change.version,
            dict(decision="accept", reason="Подтверждено вымышленным опытом."),
        )
        assert accepted.status_code == 200, accepted.text
        assert read("character-states")[0]["values"]["data"]["values"]["openness"] == 0.6
        applied = next(c for c in read("changes") if c["id"] == change.id)
        preview = post(
            "change.rollback.preview",
            change.id,
            applied["version"],
            {},
            schema="change.rollback-args",
        )
        assert preview.status_code == 200, preview.text
        plan = preview.json()
        execute = dict(
            type="change.rollback.execute",
            request_id="controls-rollback",
            scope=work,
            target=change.id,
            expected_version=plan["version"],
            arguments=dict(plan_id=plan["resource_id"], plan_version=plan["version"]),
        )
        rolled = http.post("/api/v1/commands", json=execute, headers=headers)
        assert rolled.status_code == 200, rolled.text
        assert read("character-states")[0]["values"] == original["values"]
        assert len(read("owner-operations")) == 2
        assert read("character-states", test)[0]["values"] == original["values"]
        gm = read("gm-settings", test)[0]
        tuned = post("gm.configure", "gm-runtime", gm["version"], {"intensity": 0.8}, scope=test)
        assert tuned.status_code == 200, tuned.text
        seen_run = read("sessions")[0]
        forged_scope = dict(
            test, agent_id="primary", run_id=seen_run["run_id"], session_id=seen_run["session_id"]
        )
        forged_edit = post(
            "gm.configure",
            "gm-runtime",
            gm["version"] + 1,
            {"intensity": 0.1},
            scope=forged_scope,
            request="forged-run",
        )
        assert forged_edit.status_code == 403
        refused = post(
            "gm.configure",
            "gm-runtime",
            gm["version"] + 1,
            {"paused": False},
            scope=test,
            request="test-cannot-unpause-work",
        )
        assert refused.status_code == 403
        assert read("sessions")[0]["gm_paused"]
        run = read("sessions")[0]
        start = http.post(
            "/api/v1/commands",
            headers=headers,
            json=dict(
                type="agent.start",
                request_id="controls-start",
                target="primary",
                scope=run["scope"],
                expected_version=run["version"],
                arguments={"autonomy": "observation"},
            ),
        )
        assert start.status_code == 200, start.text
        forged = http.post(
            "/api/v1/commands",
            headers=headers,
            json=dict(
                type="agent.start",
                request_id="cannot-start-settings",
                target="primary",
                scope=test,
                expected_version=read("sessions")[0]["version"],
                arguments={"autonomy": "observation"},
            ),
        )
        assert forged.status_code == 403
        markdown = http.get("/api/v1/persona-markdown/persona", params=test).json()["markdown"]
        markdown = markdown.replace("Спокойный живой русский.", "Новый вымышленный тестовый стиль.")
        markdown = markdown.replace('"openness": 0.5', '"openness": 0.2')
        validation = dict(
            type="persona.validate",
            request_id="settings-validate",
            scope=test,
            target="persona",
            expected_version=1,
            arguments=dict(schema_id="persona.validate-args", data=dict(markdown=markdown)),
        )
        checked = http.post("/api/v1/personas/persona/validate", json=validation, headers=headers)
        assert checked.status_code == 200 and checked.json()["ready"], checked.text
        applied_profile = post(
            "persona.apply",
            "persona",
            1,
            dict(markdown=markdown, revision_digest=checked.json()["digest"]),
            scope=test,
        )
        assert applied_profile.status_code == 200, applied_profile.text
        assert not read("sources", test) and not read("episodes", test)
        assert read("sessions")[0]["state"] == "ACTIVE"
        run = read("sessions")[0]
        stop = http.post(
            "/api/v1/agents/primary/stop",
            headers=headers,
            json=dict(
                type="agent.stop", request_id="controls-stop", target="primary", scope=run["scope"]
            ),
        )
        assert stop.status_code == 200, stop.text
        assert read("sessions")[0]["state"] == "STOPPED"
        version = read("gm-settings")[0]["version"]
        transfer = dict(
            type="laboratory.transfer_settings",
            request_id="controls-transfer-preview",
            target="laboratory",
            scope=work,
            expected_version=version,
            arguments=dict(
                schema_id="laboratory.transfer-settings-args",
                data=dict(
                    action="preview",
                    source_scope=test,
                    keys=["profile", "gm_intensity", "story_rules_version"],
                    operation_id="controls-transfer",
                ),
            ),
        )
        reviewed = http.post("/api/v1/laboratory/transfer-preview", json=transfer, headers=headers)
        assert reviewed.status_code == 200 and not reviewed.json()["blocked"], reviewed.text
        plan = reviewed.json()
        transfer["request_id"] = "controls-transfer-apply"
        transfer["arguments"]["data"].update(
            action="apply",
            preview_digest=plan["preview_digest"],
            plan_id=plan["id"],
            plan_version=plan["version"],
        )
        result = http.post("/api/v1/commands", json=transfer, headers=headers)
        assert result.status_code == 200, result.text
        assert read("gm-settings")[0]["intensity"] == 0.8
        assert read("personas")[0]["style"] == "Новый вымышленный тестовый стиль."
        assert read("personas")[0]["initial_values"]["data"]["values"]["openness"] == 0.2
        assert read("character-states")[0]["values"] == original["values"]
        again = http.post("/api/v1/commands", json=transfer, headers=headers)
        assert again.status_code == 200 and again.json() == result.json()
        assert read("permissions") == permissions
        assert len(read("sources")) == 1 and not read("sources", test)
        assert read("settings-transfers")[0]["status"] == "completed"
        denied = post(
            "permission.edit",
            permissions[0]["id"],
            permissions[0]["version"],
            {},
            schema="m3.empty",
        )
        assert denied.status_code >= 400
    with Server(root, port, log) as server, server.client() as http:
        http.post(
            "/api/v1/auth/login", json={"password": password}, headers={"Origin": server.origin}
        )
        assert http.get("/api/v1/sessions", params=work).json()["items"][0]["state"] == "STOPPED"
        assert http.get("/api/v1/gm-settings", params=work).json()["items"][0]["intensity"] == 0.8
    assert password not in log.read_text(errors="replace")


def test_changing_mode_in_config_does_not_make_serve_provision_rights(tmp_path):
    from controls_support import grants_snapshot

    root = tmp_path / "host"
    assert init(root, "--test-scopes", "1").returncode == 0
    before = grants_snapshot(root)
    config = json.loads((root / "host.json").read_text())
    config.update(operator_controls=True, settings_space_id="unprepared-settings-test")
    (root / "host.json").write_text(json.dumps(config))
    password = (root / "secrets" / "operator-password.txt").read_text().strip()
    test = dict(
        workspace_id="local",
        persona_id="persona",
        memory_space_id="unprepared-settings-test",
        memory_mode="test",
    )
    with Server(root, free_port(), tmp_path / "log") as server, server.client() as http:
        login = http.post(
            "/api/v1/auth/login", json={"password": password}, headers={"Origin": server.origin}
        )
        assert login.status_code == 200
        headers = {"Origin": server.origin, "X-CSRF-Token": login.json()["csrf_token"]}
        denied = http.post(
            "/api/v1/commands",
            headers=headers,
            json=dict(
                type="gm.configure",
                target="gm-runtime",
                scope=test,
                expected_version=1,
                request_id="cannot-gain-rights",
                arguments=dict(schema_id="gm.configure-args", data={"intensity": 0.9}),
            ),
        )
        assert denied.status_code == 403
    assert grants_snapshot(root) == before
