"""W3b: the daily traffic budget per persona and the operator's view of the local log.

No Docker: the log is written by the host's own writer; the gateway gets the remainder.
"""

import json
from datetime import UTC, datetime

import pytest
from ai_agent_adapters.sandbox.egress import EgressLimits
from ai_agent_contracts.common import Scope

from ai_agent_core.local_host.layout import WorkSessionsConfig
from ai_agent_core.m3_composition.work_session_egress import (
    EgressSettings,
    log_view,
    log_writer,
    resident_factory,
    used_today,
)

IMAGE = "sha256:" + "a" * 64
SCOPE = Scope(
    workspace_id="synthetic", persona_id="alpha", memory_space_id="alpha-work", memory_mode="work"
)


def settings(tmp_path, daily=10_000):
    return EgressSettings(
        "workplace.internet", IMAGE, EgressLimits(max_bytes=4000), tmp_path, daily
    )


def test_daily_remainder_lowers_the_gateway_limit_per_persona(tmp_path):
    egress = settings(tmp_path)
    write = log_writer(egress, "alpha", "attempt-1")
    write(
        {"method": "CONNECT", "target": "example.com:443", "status": "ok", "up": 3000, "down": 4000}
    )
    log_writer(egress, "beta", "attempt-2")(
        {"method": "GET", "status": "ok", "up": 0, "down": 99999}
    )
    assert used_today(egress, "alpha") == 7000
    assert used_today(egress, "alpha", datetime(2000, 1, 1, tzinfo=UTC)) == 0

    gateway = resident_factory(egress, "alpha")()
    assert gateway.remaining == 3000  # below the per-turn 4000: it becomes the cap
    write({"method": "GET", "status": "ok", "up": 0, "down": 5000})
    assert resident_factory(egress, "alpha")().remaining == -2000  # spent: the proxy gets 0
    assert resident_factory(settings(tmp_path, None), "alpha")().remaining is None


def test_operator_view_is_this_personas_day_newest_first(tmp_path):
    egress = settings(tmp_path)
    write = log_writer(egress, "alpha", "attempt-1")
    for index in range(205):
        write({"method": "GET", "target": f"http://example.com/{index}", "status": "ok",
               "up": 1, "down": 1, "seconds": 0.1})  # fmt: skip
    write({"method": "CONNECT", "target": "10.0.0.1:443", "status": "denied",
           "reason": "EGRESS_DESTINATION_DENIED", "up": 0, "down": 0, "seconds": 0.0})  # fmt: skip
    log_writer(egress, "beta", "x")({"method": "GET", "target": "http://beta/", "status": "ok"})
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    path = tmp_path / (day + ".jsonl")
    with path.open("a", encoding="utf-8") as stream:
        stream.write("not json\n" + json.dumps({"persona": "alpha", "status": "weird"}) + "\n")

    view = log_view(egress, SCOPE, day)
    assert view.enabled and view.requests == 207 and view.denied == 1
    assert view.used_bytes == 410 and view.daily_limit_bytes == 10_000
    assert len(view.items) == 200 and not view.complete
    assert view.items[0].status == "denied" and view.items[0].reason == "EGRESS_DESTINATION_DENIED"
    assert all("beta" not in item.target for item in view.items)

    assert log_view(egress, SCOPE, "2000-01-01").requests == 0
    off = log_view(None, SCOPE, day)
    assert not off.enabled and off.items == []
    with pytest.raises(ValueError):
        log_view(egress, SCOPE, "../../etc")


def test_daily_budget_bounds():
    base = dict(image_id=IMAGE, areas=("projects",), output_area="projects")
    assert WorkSessionsConfig.model_validate(base).egress_daily_mb == 2048
    assert WorkSessionsConfig.model_validate({**base, "egress_daily_mb": 0}).egress_daily_mb == 0
    with pytest.raises(ValueError):
        WorkSessionsConfig.model_validate({**base, "egress_daily_mb": -1})


def test_log_route_through_the_operator_surface(tmp_path, monkeypatch):
    import asyncio

    from test_activity import WorkModel, prepared
    from test_http import scope_params

    async def scenario():
        host = await prepared(tmp_path, monkeypatch, WorkModel())
        async with host.open():
            rt, scope = host.runtime, host.scope
            day = datetime.now(UTC).strftime("%Y-%m-%d")
            route = "/api/v1/egress-log/" + day
            off = await host.env.client.get(route, params=scope_params(scope))
            assert off.status_code == 200 and off.json()["enabled"] is False
            egress = settings(tmp_path / "egress")
            rt.session_egress = egress
            log_writer(egress, scope.persona_id, "attempt-1")(
                {"method": "GET", "target": "http://example.com/a", "status": "ok",
                 "up": 10, "down": 20, "seconds": 0.2}
            )  # fmt: skip
            view = (await host.env.client.get(route, params=scope_params(scope))).json()
            assert view["enabled"] and view["used_bytes"] == 30
            assert view["items"][0]["target"] == "http://example.com/a"
            bad = await host.env.client.get(
                "/api/v1/egress-log/yesterday", params=scope_params(scope)
            )
            assert bad.status_code == 400

    asyncio.run(scenario())
