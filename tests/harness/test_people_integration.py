"""The book at its doors: tools, chats, Telegram comments, the game, the operator's CLI."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from ai_agent_adapters.stream.chat_model import ChatMessage
from ai_agent_contracts.states import RunState

from ai_agent_core.local_host.layout import HostError
from ai_agent_core.local_host.people_cli import run as people_cli
from ai_agent_core.m3_composition.harness_chat import ChatHub
from ai_agent_core.m3_composition.harness_comments import TelegramComments
from ai_agent_core.m3_composition.harness_game import GameSource
from ai_agent_core.m3_composition.harness_mcp import PlatformMcp
from ai_agent_core.m3_composition.harness_people import PeopleBook, PeopleSource


@pytest.fixture
def book(tmp_path):
    return PeopleBook(tmp_path / "people.sqlite")


def runtime(state=RunState.ACTIVE):
    snapshot = SimpleNamespace(state=state, control_epoch=3, scope=None)
    return SimpleNamespace(supervisor=SimpleNamespace(snapshot=snapshot), tickets=None)


def call(mcp, tool, **arguments):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    return asyncio.run(mcp.handle(body))["result"]["content"][0]["text"]


def test_tools_exist_only_with_a_book_and_follow_the_run(book):
    listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    names = lambda m: {t["name"] for t in asyncio.run(m.handle(listing))["result"]["tools"]}  # noqa: E731
    assert not [n for n in names(PlatformMcp(runtime(), lambda: 3)) if n.startswith("person_")]  # type: ignore[arg-type]
    mcp = PlatformMcp(runtime(), lambda: 3, people=book)  # type: ignore[arg-type]
    assert {n for n in names(mcp) if n.startswith("person_")} == {
        "person_find", "person_recall", "person_note", "person_met", "person_link",
    }  # fmt: skip
    assert call(PlatformMcp(runtime(), lambda: 3), "person_find", query="x") == (
        "Отказ: CAPABILITY_UNAVAILABLE"
    )  # type: ignore[arg-type]
    stopped = PlatformMcp(runtime(RunState.STOPPING), lambda: 3, people=book)  # type: ignore[arg-type]
    assert call(stopped, "person_find", query="x") == "Отказ: STALE_CONTEXT"


def test_she_meets_notes_and_recalls_by_audience(book):
    mcp = PlatformMcp(runtime(), lambda: 3, people=book)  # type: ignore[arg-type]
    assert call(mcp, "person_find", query="Иван") == "Таких не знаю."
    met = json.loads(call(mcp, "person_met", platform="call", name="Иван"))
    person = met["person_id"]
    assert (
        call(mcp, "person_note", person_id=person, text="врач", platform="call", private=True)
        == "Записано."
    )
    call(mcp, "person_note", person_id=person, text="любит джаз", platform="call", private=False)
    assert json.loads(call(mcp, "person_find", query="иван"))[0]["person_id"] == person
    public = json.loads(
        call(mcp, "person_recall", person_id=person, where="public", platform="call")
    )
    assert [f["text"] for f in public["facts"]] == ["любит джаз"]
    private = json.loads(call(mcp, "person_recall", person_id=person, where="private"))
    assert len(private["facts"]) == 2
    assert (
        call(mcp, "person_recall", person_id=person, where="nowhere") == "Отказ: INVALID_ARGUMENTS"
    )
    assert (
        call(mcp, "person_recall", person_id="p-nobody", where="private")
        == "Такого человека не знаю."
    )
    assert "Не получилось" in call(
        mcp, "person_note", person_id="p-nobody", text="x", platform="call"
    )


def test_a_link_code_is_given_only_privately_and_a_suspicion_identifies_nobody(book):
    mcp = PlatformMcp(runtime(), lambda: 3, people=book)  # type: ignore[arg-type]
    a, b = book.seen("telegram", "1", "Ann"), book.seen("twitch", "2", "annie")
    assert "только лично" in call(mcp, "person_link", action="code", person_id=a)
    given = call(mcp, "person_link", action="code", person_id=a, delivery="private")
    assert "LINK-" in given and "лично" in given
    out = call(
        mcp,
        "person_link",
        action="suspect",
        person_id=a,
        other_person_id=b,
        basis="оба про шахматы",
    )
    assert "никого не объединяет" in out and a != b and len(book.suspicions()) == 1
    assert "Нужны два" in call(
        mcp, "person_link", action="suspect", person_id=a, other_person_id="p-x", basis="y"
    )


def message(platform, id_, author_id, author, text):
    return ChatMessage(platform, id_, author_id, author, text, 1.0)


async def allow(prompt):
    return True, ""


def hub(tmp_path, book):
    return ChatHub(
        {}, ["Опечатка"], tmp_path / "chat.jsonl", allow, lambda: "", people=book,
    )  # fmt: skip


def test_a_mention_in_a_stream_chat_brings_what_is_safe_to_recall_and_a_code_joins_accounts(
    tmp_path, book
):
    telegram = book.seen("telegram", "555", "Zoya")
    book.note(telegram, "работает врачом", platform="telegram", private=True)
    book.note(telegram, "любит шахматы", platform="telegram", private=False)
    h = hub(tmp_path, book)
    code = book.issue_code(telegram)
    h._take(message("twitch", "m1", "42", "zoya_tw", f"Опечатка, это я! {code}"))  # noqa: SLF001
    assert book.lookup("twitch", "42") == telegram  # proven by the code
    ((key, line),) = h.take_called()
    assert "любит шахматы" in line and "врач" not in line  # public facts only
    assert "виделись раз" in line
    rows = json.loads(h.read(0, 10))["messages"]
    assert rows[0]["person"] == telegram
    notices = asyncio.run(PeopleSource(book).poll(None, lambda: None))
    assert len(notices) == 1 and "Теперь это один человек" in notices[0].text
    assert asyncio.run(PeopleSource(book).poll(None, lambda: None)) == []


def test_a_nickname_alone_never_joins_chat_and_telegram(tmp_path, book):
    telegram = book.seen("telegram", "555", "Zoya")
    h = hub(tmp_path, book)
    h._take(message("twitch", "m1", "42", "Zoya", "Опечатка, привет!"))  # noqa: SLF001
    assert book.lookup("twitch", "42") != telegram and book.lookup("twitch", "42") is not None


def test_telegram_commenters_are_recorded_and_a_code_in_a_comment_counts(book):
    comments = object.__new__(TelegramComments)
    comments.people = book
    first = book.seen("minecraft", "Zoya", "Zoya")
    code = book.issue_code(first)
    author = {"telegram_user_id": 777, "username": "zoya", "name": "Зоя"}
    comments._remember({"author": author, "text": f"привет {code}"})  # noqa: SLF001
    assert book.lookup("telegram", "777") == first
    comments._remember({"author": {"telegram_user_id": None}, "text": "аноним"})  # noqa: SLF001
    comments.people = None
    comments._remember({"author": author, "text": "ещё"})  # noqa: SLF001 - no book: nothing


def chat_source(lines_per_poll, book):
    step = {"i": -1}

    def call_mod(tool, arguments):
        if tool == "getGameState":
            step["i"] += 1
            return {
                "inGame": True,
                "self": {
                    "hp": 20,
                    "maxHp": 20,
                    "food": 20,
                    "pos": "0,64,0",
                    "dimension": "overworld",
                },
                "players": [],
            }
        if tool == "getRecentChat":
            return lines_per_poll[min(step["i"], len(lines_per_poll) - 1)]
        return []

    return GameSource("http://x/mcp", None, ["Опечатка"], call=call_mod, book=book)


def test_a_minecraft_player_is_recorded_known_again_and_can_prove_who_they_are(book):
    telegram = book.seen("telegram", "555", "Zoya")
    book.note(telegram, "строит замки", platform="minecraft", private=False)
    code = book.issue_code(telegram)
    src = chat_source([["<Eve> hi"], ["<Eve> hi", f"<Zoya> Опечатка, это я {code}"]], book)
    poll = lambda: asyncio.run(src.poll(None, lambda: None))  # noqa: E731
    poll()
    [event] = [e for e in poll() if e.kind == "talk"]
    assert book.lookup("minecraft", "zoya") == telegram  # the code was typed in the game chat
    assert event.key == "Zoya" and book.lookup("minecraft", "eve") is None  # only talkers are met


def test_the_operator_cli_lists_links_splits_and_erases(tmp_path, capsys):
    root = tmp_path / "host"
    data = root / "data" / "harness"
    book = PeopleBook(data / "people.sqlite")
    a = book.seen("telegram", "1", "Ann")
    book.seen("twitch", "2", "annie")
    assert people_cli(root, "list", [], yes=False) == 0
    assert "telegram:1" in capsys.readouterr().out
    people_cli(root, "link", ["telegram:1", "twitch:2"], yes=False)
    assert book.lookup("twitch", "2") == a
    people_cli(root, "split", ["twitch:2"], yes=False)
    assert book.lookup("twitch", "2") != a
    people_cli(root, "show", [a], yes=False)
    assert "Ann" in capsys.readouterr().out
    with pytest.raises(HostError) as error:
        people_cli(root, "erase", [a], yes=False)
    assert "ERASE_NEEDS_YES" in repr(error.value)
    people_cli(root, "erase", [a], yes=True)
    assert book.lookup("telegram", "1") is None
    assert book.lookup("twitch", "2") is not None  # the split-off account was not erased
    with pytest.raises(HostError):
        people_cli(root, "show", ["telegram:1"], yes=False)
    with pytest.raises(HostError):
        people_cli(tmp_path / "other", "list", [], yes=False)


def test_the_prompt_teaches_the_people_rules_only_when_the_book_exists(tmp_path, book):
    from ai_agent_core.m3_composition.harness_host import HarnessHost, HarnessSettings

    settings = HarnessSettings(
        image_id="i", port=4096, password="p", upstream="https://x/v1", key="k", model="m",
        usage_dir=tmp_path, daily_budget=1.0, input_rate=0.1, output_rate=0.1,
    )  # fmt: skip
    host = HarnessHost.__new__(HarnessHost)
    host.settings = settings
    scope = SimpleNamespace(persona_id="p")
    host.rt = SimpleNamespace(supervisor=SimpleNamespace(snapshot=SimpleNamespace(scope=scope)))
    without = str(host.config("http://x:8080/v1", "base", False))
    host.people = book
    with_book = str(host.config("http://x:8080/v1", "base", False))
    assert "## People" not in without and "## People" in with_book
    assert "A shared nickname proves nothing" in with_book


def test_old_player_notes_move_into_the_book_once(book, tmp_path):
    from ai_agent_core.m3_composition.harness_people import import_notes

    text = (
        "# мои заметки\n"
        "Bob: likes parkour\n"
        "- Eve — builds castles\n"
        "Bob » has a red cat\n"
        "странная строка без ника\n"
        "Zo: слишком короткий ник\n"
        "\n"
    )
    assert import_notes(book, text) == (3, 2)
    bob = book.lookup("minecraft", "bob")
    assert [f["text"] for f in book.card(bob, public=True, platform="minecraft")["facts"]] == [
        "likes parkour",
        "has a red cat",
    ]  # they were public in the game chat before, and stay so
    assert import_notes(book, text) == (3, 2)  # again: nothing is added twice
    assert len(book.card(bob, public=False)["facts"]) == 2
    book.erase(bob, by="operator")
    assert import_notes(book, "Bob: back again\n") == (0, 1)  # an erased player stays erased


def test_the_cli_imports_her_notes_file(tmp_path):

    root = tmp_path / "host"
    root.mkdir()
    notes = tmp_path / "her" / "p" / "playbook"
    notes.mkdir(parents=True)
    (notes / "people.md").write_text("Bob: likes parkour\n", encoding="utf-8")
    book = PeopleBook(root / "data" / "harness" / "people.sqlite")
    with pytest.raises(HostError):  # the file is named explicitly, the default needs a config
        people_cli(root, "import-notes", [str(tmp_path / "missing.md")], yes=False)
    people_cli(root, "import-notes", [str(notes / "people.md")], yes=False)
    assert book.lookup("minecraft", "bob") is not None
    link = notes / "link.md"
    link.symlink_to(notes / "people.md")
    with pytest.raises(HostError):
        people_cli(root, "import-notes", [str(link)], yes=False)
