"""Real local host + browser: the operator handles tickets in plain steps while STOPPED.

A question: read it, answer with "Отправить ответ", then finish it separately — no right. An
access request: "Разрешить" is filled from current records (capability, binding, versions);
a decision above the host ceiling is explained, a lower one is previewed and confirmed; another
request is refused without a right. Synthetic data, mock provider, no paid calls.
"""

import asyncio
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.storage import Owner
from ai_agent_application.tickets import TicketDraft, TicketService
from ai_agent_contracts.agent_workplace import CapabilityRequest, Ticket
from ai_agent_contracts.api import Permission
from host_support import Server, command, free_port, init
from playwright.sync_api import expect

from ai_agent_core.local_host import rights
from ai_agent_core.local_host.init import work_scope
from ai_agent_core.local_host.layout import load_config, paths
from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import PRINCIPALS

ROOT = Path(__file__).resolve().parents[2]
TITLE = "Нужен общий справочник"


async def owner(root, action):
    host = paths(root)
    config = load_config(host)
    scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
    store = SQLiteStore(host.database, extensions=new_registry())
    try:
        service = TicketService(
            store,
            lambda: datetime.now(UTC),
            readers=rights.ticket_readers(),
            principal=PRINCIPALS[Owner.TICKETS],
        )
        return await action(service, store, scope)
    finally:
        await store.close()


async def seed(service, store, scope):
    draft = TicketDraft(
        kind="capability_request",
        title=TITLE,
        summary="Для конспекта нужен справочник.",
        attempted="Искала в своих заметках.",
        decision_needed="Выдать справочник или отказать.",
        basis="reference.read",
        request=CapabilityRequest(capability_id="reference.read", constraints="Только чтение."),
    )
    await service.raise_ticket("seed", scope, "service", "workplace", draft, "Справочника нет.")


async def seed_question(service, store, scope):
    draft = TicketDraft(
        kind="question",
        title=TITLE,
        summary="Для конспекта не хватает справочника.",
        attempted="Искала в своих заметках.",
        decision_needed="Скажите, где взять справочник или как обойтись без него.",
        basis="where-reference",
    )
    await service.raise_ticket("seed", scope, "agent", "workplace-agent", draft, "Где справочник?")


async def final(service, store, scope):
    [ticket] = await store.repository(Ticket, PRINCIPALS[Owner.TICKETS], scope).list()
    permissions = await store.repository(Permission, PRINCIPALS[Owner.WORKSPACE], scope).list()
    return (
        ticket.state,
        ticket.next_turn,
        ticket.message_count,
        [p.capability_id for p in permissions],
    )


def in_thread(call):
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(call)).result()


def login(page, server, root):
    page.goto(server.origin + "/#/tickets")
    page.get_by_label("Пароль", exact=True).fill(
        (root / "secrets/operator-password.txt").read_text().strip()
    )
    page.get_by_role("button", name="Войти", exact=True).click()


def test_operator_answers_a_question_and_finishes_it_without_a_right(page, tmp_path):
    root = tmp_path / "host"
    assert init(root).returncode == 0
    enabled = subprocess.run(
        command("enable-tickets", "--root", str(root)), capture_output=True, text=True, timeout=120
    )
    assert enabled.returncode == 0, enabled.stderr
    in_thread(owner(root, seed_question))
    before = in_thread(owner(root, final))[3]  # rights prepared by init, if any
    with Server(
        root, free_port(), tmp_path / "server.log", panel_dir=ROOT / "apps/panel/dist"
    ) as server:
        login(page, server, root)
        mine = page.get_by_role("list", name="Нужен мой ответ")
        expect(mine.get_by_text(TITLE)).to_be_visible(timeout=10000)
        expect(mine.get_by_text("новое")).to_be_visible()
        mine.get_by_role("button").first.click()
        card = page.get_by_label("Карточка обращения")
        expect(card.get_by_text("Что случилось")).to_be_visible()
        expect(card.get_by_text("Скажите, где взять справочник", exact=False)).to_be_visible()
        expect(card.get_by_text("Где справочник?")).to_be_visible()
        expect(card.get_by_text("Ждёт вашего ответа").first).to_be_visible()
        # Technical details are folded away.
        expect(card.get_by_text("Идентификатор:", exact=False)).to_be_hidden()
        expect(mine.get_by_text("новое")).to_have_count(0, timeout=10000)
        card.get_by_role("button", name="Отправить ответ").click()
        expect(card.get_by_role("alert")).to_contain_text("Напишите сообщение.")
        card.get_by_label("Ваш ответ").fill("Возьми общий справочник из раздела «Справочники».")
        card.get_by_role("button", name="Отправить ответ").click()
        expect(card.get_by_role("status")).to_contain_text("Ответ отправлен", timeout=10000)
        expect(card.get_by_role("status")).to_contain_text("после явного START")
        working = page.get_by_role("list", name="В работе")
        expect(working.get_by_text(TITLE)).to_be_visible(timeout=10000)
        expect(card.get_by_text("Агент работает над этим").first).to_be_visible()
        # Finishing is a separate, explained step; it grants nothing.
        card.get_by_role("button", name="Завершить обращение…").click()
        closing = card.get_by_label("Завершение обращения")
        expect(closing.get_by_text("Никаких прав это не выдаёт", exact=False)).to_be_visible()
        closing.get_by_role("button", name="Завершить", exact=True).click()
        expect(card.get_by_role("status")).to_contain_text("Обращение завершено", timeout=10000)
        expect(page.get_by_role("list", name="Завершённые").get_by_text(TITLE)).to_be_visible(
            timeout=10000
        )
        expect(page.get_by_text("Остановлен").first).to_be_visible()
    state, turn, count, capabilities = in_thread(owner(root, final))
    assert (state, turn, count) == ("resolved", None, 3)
    assert capabilities == before  # finishing wrote no right


async def seed_capability(service, store, scope):
    from ai_agent_contracts.agent_workplace import CapabilityBinding, CapabilityManifest

    from ai_agent_core.m3_composition.state import put

    now = datetime.now(UTC)
    base = dict(scope=scope, created_at=now, updated_at=now, version=1)
    registry = new_registry()
    readers = rights.record_readers()
    manifest = CapabilityManifest(
        id="manifest-reference",
        capability_id="reference.read",
        capability_version=1,
        summary="Чтение общего справочника.",
        instructions="Только чтение.",
        input_schema_id="reference.query",
        output_schema_id="reference.page",
        effect="none",
        areas=["reference"],
        network="none",
        runner_required=False,
        **base,
    )
    binding = CapabilityBinding(
        id="binding-reference",
        binding_id="binding-reference",
        capability_id="reference.read",
        capability_version=1,
        adapter_id="reference-files",
        adapter_version="1",
        state="pending",
        **base,
    )
    await put(store, registry, Owner.WORKSPACE, scope, "m", manifest, None, readers)
    await put(store, registry, Owner.WORKSPACE, scope, "b", binding, None, readers)
    await seed(service, store, scope)


def capability_host(tmp_path):
    from ai_agent_core.local_host.layout import PermissionCeiling, save_config

    root = tmp_path / "host"
    assert init(root, "--operator-controls").returncode == 0
    enabled = subprocess.run(
        command("enable-tickets", "--root", str(root)), capture_output=True, text=True, timeout=120
    )
    assert enabled.returncode == 0, enabled.stderr
    host = paths(root)
    ceiling = {"reference.read": PermissionCeiling(level="prepare_only", max_days=30)}
    save_config(host, load_config(host).model_copy(update={"workplace_permissions": ceiling}))
    in_thread(owner(root, seed_capability))
    return root


async def granted(service, store, scope):
    rows = await store.repository(Permission, PRINCIPALS[Owner.WORKSPACE], scope).list()
    return [(p.capability_id, p.level, p.revoked_at is None) for p in rows]


async def repeat_request(service, store, scope):
    # The same request again from the agent: the owner merges it and the ticket version moves.
    draft = TicketDraft(
        kind="capability_request",
        title=TITLE,
        summary="Для конспекта нужен справочник.",
        attempted="Искала в своих заметках.",
        decision_needed="Выдать справочник или отказать.",
        basis="reference.read",
        request=CapabilityRequest(capability_id="reference.read", constraints="Только чтение."),
    )
    await service.raise_ticket("again", scope, "service", "workplace", draft, "Всё ещё нужен.")


def test_operator_allows_a_request_after_preview_within_the_host_ceiling(page, tmp_path):
    root = capability_host(tmp_path)
    before = in_thread(owner(root, granted))
    with Server(
        root, free_port(), tmp_path / "server.log", panel_dir=ROOT / "apps/panel/dist"
    ) as server:
        login(page, server, root)
        expect(page.get_by_label("Профиль обращений")).to_contain_text("рабочая память")
        page.get_by_role("list", name="Нужен мой ответ").get_by_role("button").first.click()
        request = page.get_by_label("Запрос доступа")
        expect(request.get_by_text("Нужна возможность: Чтение общего справочника.")).to_be_visible(
            timeout=10000
        )
        expect(request.get_by_text("Области: Справочники")).to_be_visible()
        expect(request.get_by_text("Срок: 7 дн.", exact=False)).to_be_visible()
        expect(request.get_by_text("ещё не готово", exact=False)).to_be_visible()
        # No identifier is typed: capability, binding and versions come from current records.
        expect(request.get_by_role("textbox")).to_have_count(0)
        # The default (automatic) is above this host's ceiling: the reason and the next step.
        request.get_by_role("button", name="Разрешить…").click()
        expect(request.get_by_role("alert")).to_contain_text("потолок", timeout=10000)
        expect(request.get_by_role("alert")).to_contain_text("Подробностях решения")
        request.get_by_text("Подробности решения").click()
        request.get_by_text("Только подготовка").click()
        request.get_by_role("button", name="Разрешить…").click()
        consequences = request.get_by_label("Последствия решения")
        expect(consequences).to_contain_text("не запускает агента", timeout=10000)
        expect(consequences).to_contain_text("подключение ещё не готово")
        granted_view = consequences.get_by_label("Что вы разрешаете")
        expect(granted_view).to_contain_text("Области: Справочники")
        expect(granted_view).to_contain_text("уровень: Только подготовка")
        expect(granted_view).to_contain_text("Ограничения: Только чтение.")
        # The agent adds to the ticket between preview and confirmation: the server refuses
        # the stale decision; the panel explains, keeps the choice and offers a refresh.
        in_thread(owner(root, repeat_request))
        consequences.get_by_role("button", name="Подтвердить выдачу доступа").click()
        problem = request.get_by_role("alert")
        expect(problem).to_contain_text("изменилось", timeout=10000)
        problem.get_by_role("button", name="Обновить карточку").click()
        expect(request.get_by_role("button", name="Разрешить…")).to_be_enabled(timeout=10000)
        assert in_thread(owner(root, granted)) == before  # the stale decision wrote nothing
        expect(request.get_by_role("radio", name="Только подготовка")).to_be_checked()
        request.get_by_role("button", name="Разрешить…").click()
        consequences = request.get_by_label("Последствия решения")
        consequences.get_by_role("button", name="Подтвердить выдачу доступа").click()
        card = page.get_by_label("Карточка обращения")
        expect(card.get_by_role("status")).to_contain_text("Доступ выдан до", timeout=10000)
        expect(request.get_by_text("Доступ уже выдан до", exact=False)).to_be_visible(timeout=10000)
        # Opening, answering and deciding never started the agent.
        expect(page.locator(".active-run")).to_contain_text("Остановлен")
    rights = in_thread(owner(root, granted))
    assert ("reference.read", "prepare_only", True) in rights
    assert len(rights) == len(before) + 1  # exactly one decision; the stale one wrote nothing


def test_operator_refuses_a_request_and_no_right_is_written(page, tmp_path):
    root = capability_host(tmp_path)
    before = in_thread(owner(root, granted))
    with Server(
        root, free_port(), tmp_path / "server.log", panel_dir=ROOT / "apps/panel/dist"
    ) as server:
        login(page, server, root)
        page.get_by_role("list", name="Нужен мой ответ").get_by_role("button").first.click()
        card = page.get_by_label("Карточка обращения")
        expect(card.get_by_text("Ответ текстом не выдаёт доступ", exact=False)).to_be_visible(
            timeout=10000
        )
        card.get_by_role("button", name="Отказать…").click()
        refusal = card.get_by_label("Отказ в доступе")
        expect(refusal.get_by_text("без выдачи доступа", exact=False)).to_be_visible()
        refusal.get_by_label("Комментарий для агента (необязательно)").fill("Справочник не нужен.")
        refusal.get_by_role("button", name="Отказать в доступе").click()
        expect(card.get_by_role("status")).to_contain_text(
            "Запрос закрыт без выдачи доступа", timeout=10000
        )
        expect(card.get_by_text("Закрыто без выдачи доступа").first).to_be_visible(timeout=10000)
        expect(page.get_by_label("Запрос доступа")).to_have_count(0)
    state, turn, _, _ = in_thread(owner(root, final))
    assert (state, turn) == ("closed_unfulfilled", None)
    after = in_thread(owner(root, granted))
    assert after == before and not any(c == "reference.read" for c, _, _ in after)
