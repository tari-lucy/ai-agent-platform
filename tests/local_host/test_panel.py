"""`serve --panel-dir`: build checks, confined static serving, API unchanged (synthetic build)."""

import http.client
import os
import subprocess

import pytest
from host_support import Server, command, free_port, init, wait_for  # noqa: F401

INDEX = (
    "<!doctype html><html><head><title>Synthetic panel</title>"
    '<script type="module" src="/assets/app.js"></script>'
    '<link rel="stylesheet" href="/assets/app.css"></head>'
    '<body><div id="root"></div></body></html>'
)


@pytest.fixture
def build(tmp_path):
    dist = tmp_path / "site" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX)
    (dist / "assets" / "app.js").write_text("console.log('synthetic');")
    (dist / "assets" / "app.css").write_text("body{}")
    (dist / ".env").write_text("HIDDEN=1")
    (tmp_path / "site" / "neighbour.txt").write_text("NEIGHBOUR-MARKER")
    (tmp_path / "outside.txt").write_text("OUTSIDE-MARKER")
    os.symlink(tmp_path / "outside.txt", dist / "leak.txt")
    return dist


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "host"
    assert init(root).returncode == 0
    return root, (root / "secrets" / "operator-password.txt").read_text().strip()


def serve_once(root, *arguments):
    return subprocess.run(
        command("serve", "--root", str(root), "--provider", "mock", *arguments),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_bad_builds_are_refused_with_a_code(host, build, tmp_path):
    root, _ = host
    cases = {
        "relative/dist": "PANEL_DIR_NOT_ABSOLUTE",
        str(tmp_path / "absent"): "PANEL_DIR_MISSING",
        str(root): "PANEL_DIR_OVERLAPS_HOST",  # the data directory itself
        str(root / "data"): "PANEL_DIR_OVERLAPS_HOST",
        str(tmp_path): "PANEL_DIR_OVERLAPS_HOST",  # contains the host directory
    }
    for value, code in cases.items():
        done = serve_once(root, "--panel-dir", value)
        assert done.returncode == 78 and code in done.stderr, (code, done.stderr)
    (build / "assets" / "app.js").unlink()  # index refers to a file that is gone
    assert "PANEL_BUILD_INCOMPLETE" in serve_once(root, "--panel-dir", str(build)).stderr
    (build / "index.html").unlink()
    assert "PANEL_BUILD_INCOMPLETE" in serve_once(root, "--panel-dir", str(build)).stderr


def raw(server, path):
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    connection.request("GET", path)
    response = connection.getresponse()
    body = response.read().decode("utf-8", "replace")
    connection.close()
    return response.status, response.getheader("content-type") or "", body


def test_panel_serving_is_confined_and_api_stays_json(host, build, tmp_path):
    root, password = host
    with Server(root, free_port(), tmp_path / "log", panel_dir=build) as server:
        status, kind, body = raw(server, "/")
        assert status == 200 and "Synthetic panel" in body and kind.startswith("text/html")
        for page in ("/laboratory", "/people/profile"):  # page navigation falls back to index
            assert "Synthetic panel" in raw(server, page)[2]
        # The old /opencode/ window is gone: 404, never the page fallback; similar names stay pages.
        for old in ("/opencode", "/opencode/", "/opencode/aw_x/session/ses_1"):
            status, _, body = raw(server, old)
            assert status == 404 and "Synthetic panel" not in body, old
        assert "Synthetic panel" in raw(server, "/opencode-notes")[2]
        status, kind, _ = raw(server, "/assets/app.js")
        assert status == 200 and "javascript" in kind
        assert raw(server, "/assets/missing.js")[0] == 404
        # Nothing outside the build, and no dotfiles or links leaving it.
        escapes = (
            "/../neighbour.txt",
            "/%2e%2e/neighbour.txt",
            "/assets/../../neighbour.txt",
            "/%2e%2e%2f%2e%2e/host/host.json",
            "/../host/secrets/operator-password.txt",
            "/host.json",
            "/data/platform.sqlite",
            "/secrets/operator-password.txt",
            "/.env",
            "/.git/config",
            "/leak.txt",
            "/assets/%2e%2e/%2e%2e/outside.txt",
            "/assets%5c..%5cindex.html",
        )
        for path in escapes:
            status, _, body = raw(server, path)
            assert "MARKER" not in body and password not in body and "HIDDEN" not in body, path
            assert status in (400, 404), (path, status)
        # Unknown API paths are JSON errors, never the panel page.
        for path in ("/api/v1/nope", "/api/v2/anything", "/internal/x"):
            status, kind, body = raw(server, path)
            assert status >= 400 and "Synthetic panel" not in body and "<html" not in body.lower()
        assert raw(server, "/api/v1/auth/session")[0] == 401  # authentication unchanged
        assert raw(server, "/assets/app.js?x=1")[0] == 400  # existing browser-surface rule


def test_without_the_parameter_there_is_no_panel(host, tmp_path):
    root, _ = host
    with Server(root, free_port(), tmp_path / "log") as server:
        status, _, body = raw(server, "/")
        assert status == 404 and "<html" not in body.lower()


def test_panel_and_api_on_one_address_smoke(host, build, tmp_path):
    root, password = host
    port, log = free_port(), tmp_path / "log"
    with Server(root, port, log, panel_dir=build) as server, server.client() as http_client:
        assert "Synthetic panel" in http_client.get("/").text
        origin = {"Origin": server.origin}
        login = http_client.post("/api/v1/auth/login", json={"password": password}, headers=origin)
        assert login.status_code == 200
        headers = {**origin, "X-CSRF-Token": login.json()["csrf_token"]}
        base = dict(
            workspace_id="local",
            persona_id="persona",
            memory_space_id="persona-work",
            memory_mode="work",
        )

        def run():
            return http_client.get("/api/v1/sessions", params=base).json()["items"][0]

        assert run()["state"] == "STOPPED"
        unknown = http_client.get("/api/v1/nope")
        assert unknown.status_code == 404 and "json" in unknown.headers["content-type"]
        assert "Synthetic panel" not in unknown.text
        current = run()
        started = http_client.post(
            "/api/v1/commands",
            headers=headers,
            json=dict(
                type="agent.start",
                request_id="panel-start",
                target="primary",
                scope=current["scope"],
                expected_version=current["version"],
                arguments={"autonomy": "autonomous"},
            ),
        )
        assert started.status_code == 200 and run()["state"] == "ACTIVE"
        stopped = http_client.post(
            "/api/v1/agents/primary/stop",
            headers=headers,
            json=dict(
                type="agent.stop", request_id="panel-stop", target="primary", scope=run()["scope"]
            ),
        )
        assert stopped.status_code == 200
        wait_for(lambda: run()["state"], lambda state: state == "STOPPED", "STOPPED")
    with Server(root, port, log, panel_dir=build) as server, server.client() as http_client:
        assert "Synthetic panel" in http_client.get("/").text
        assert http_client.get("/api/v1/sessions", params=base).status_code == 401  # no session
        http_client.post(
            "/api/v1/auth/login", json={"password": password}, headers={"Origin": server.origin}
        )
        assert http_client.get("/api/v1/sessions", params=base).json()["items"][0]["state"] == (
            "STOPPED"
        )
