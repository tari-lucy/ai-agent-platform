"""W4 home routes over production local-host HTTP; an in-memory volume stands in for Docker."""

import asyncio
import base64

from test_activity import WorkModel, prepared

from ai_agent_core.m3_composition.work_session_home import volume
from ai_agent_core.opencode_ui.protocol import PREFIX, directory


class Volumes:
    """HomeFiles stand-in: the same answers, one dict per volume name."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, bytes]] = {}

    async def list(self, name, path):
        files = self.data.get(name)
        if files is None:
            return {"status": "ok", "entries": [], "complete": True}
        prefix = path.strip("/") + "/" if path.strip("/") else ""
        rows = {}
        for key, value in files.items():
            if key.startswith(prefix):
                head, _, rest = key[len(prefix) :].partition("/")
                rows[head] = {"name": head, "type": "directory" if rest else "file",
                              "size": 0 if rest else len(value), "mtime": 0}  # fmt: skip
        if prefix and not rows:
            return {"status": "missing"}
        return {"status": "ok", "entries": list(rows.values()), "complete": True}

    async def read(self, name, path):
        value = self.data.get(name, {}).get(path.strip("/"))
        if value is None:
            return {"status": "missing"}
        return {"status": "ok", "size": len(value), "content": base64.b64encode(value).decode()}

    async def put(self, name, file, value):
        files = self.data.get(name)
        if files is None:
            return {"status": "missing"}
        if "inbox/" + file in files:
            return {"status": "exists"}
        files["inbox/" + file] = value
        return {"status": "ok"}


def test_operator_browses_the_home_and_drops_a_file_into_inbox(tmp_path, monkeypatch):
    async def scenario():
        host = await prepared(tmp_path, monkeypatch, WorkModel())
        async with host.open():
            client, rt, scope = host.env.client, host.runtime, host.scope
            base = PREFIX + "/agents/" + directory(scope)
            # Homes not enabled: the capability is unavailable, not an empty tree.
            first = await client.get(base + "/home")
            assert first.status_code == 503, first.text
            volumes = Volumes()
            rt.session_home_files = volumes
            assert (await client.get(base + "/home")).json() == {"entries": [], "complete": True}
            volumes.data[volume(scope)] = {
                "README.md": "Порядок папки".encode(),
                "projects/demo/run.bin": b"\x00\x01binary",
            }
            root = (await client.get(base + "/home")).json()["entries"]
            assert {r["name"]: r["type"] for r in root} == {
                "README.md": "file",
                "projects": "directory",
            }
            nested = await client.get(base + "/home/projects/demo")
            assert [r["name"] for r in nested.json()["entries"]] == ["run.bin"]
            readme = (await client.get(base + "/home-file/README.md")).json()
            assert readme == {"text": "Порядок папки", "size": 25, "truncated": False}
            binary = (await client.get(base + "/home-file/projects/demo/run.bin")).json()
            assert binary["text"] is None and binary["size"] == 8
            assert (await client.get(base + "/home-file/absent.txt")).status_code == 404
            assert (await client.get(base + "/home/../etc")).status_code in (400, 404)
            assert (await client.get(base + "/home?path=x")).status_code == 400

            octet = {**host.env.headers, "content-type": "application/octet-stream"}
            put = await client.post(
                base + "/home-inbox/brief.txt", content=b"Do this", headers=octet
            )
            assert put.status_code == 200, put.text
            assert put.json() == {"path": "inbox/brief.txt", "size": 7}
            assert volumes.data[volume(scope)]["inbox/brief.txt"] == b"Do this"
            again = await client.post(base + "/home-inbox/brief.txt", content=b"x", headers=octet)
            assert again.status_code == 409
            # A mutation needs the CSRF token like every other operator write.
            plain = {"content-type": "application/octet-stream"}
            refused = await client.post(base + "/home-inbox/x.txt", content=b"x", headers=plain)
            assert refused.status_code == 403
            assert (await client.get(base + "/home-inbox/x.txt")).status_code == 404
            assert rt.supervisor.snapshot.state.value == "STOPPED"

    asyncio.run(scenario())
