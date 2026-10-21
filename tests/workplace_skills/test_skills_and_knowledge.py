"""AW-03 skills and knowledge over the real AW-02 file owner (ManagedFiles).

Synthetic data only: real SQLite, deletion ledger and private blob directory from the
AW-02 sample world; the attempts are registered by the activity owner before IO.
"""

import pytest
from ai_agent_application.agent_knowledge import KnowledgeBase, NoteDraft
from ai_agent_application.agent_skills import SkillDraft, SkillLibrary
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess
from ai_agent_contracts.api import Permission
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.states import ErrorCode
from wf_support import ACT, AREA, CAT, MEM, World, run

SCOPE = AREA
SKILLS = Principal("core-skills", "core_owner", Owner.SKILLS)
WORKSPACE = Principal("core-workspace", "core_owner", Owner.WORKSPACE)


async def library_world(world: World, areas=("knowledge", "skills")):
    """The sample world plus the skills owner; the file hook allows only the given areas."""
    await world.store.provision(
        AREA,
        (
            Grant(CAT, Owner.CATALOGUE, read=True, write=True, api_transfer=True),
            Grant(CAT, Owner.MEMORY, read=True, api_transfer=True),
            Grant(CAT, Owner.ACTIVITY, read=True),
            Grant(MEM, Owner.MEMORY, read=True, write=True),
            Grant(ACT, Owner.ACTIVITY, read=True, write=True),
            Grant(ACT, Owner.MEMORY, read=True),
            Grant(SKILLS, Owner.SKILLS, read=True, write=True),
            Grant(SKILLS, Owner.MEMORY, read=True),  # evidence of a verified skill
            Grant(WORKSPACE, Owner.WORKSPACE, read=True),
        ),
    )

    async def authorize(admission, operation, area):
        if not world.allowed or area not in areas:
            raise ContractError(ErrorCode.ACCESS_DENIED)

    world.files.authorize = authorize
    # Evidence the skills owner may cite: a source shared with it by the memory owner.
    world.memory.reader_access = (*world.memory.reader_access, RecordAccess(SKILLS.id))
    return SkillLibrary(world.store, world.files, world.clock), KnowledgeBase(world.files)


SUMMARY = SkillDraft(
    skill_id="summarize-long-text",
    title="Конспект длинного текста",
    applicability="Длинный текст статьи нужно сжать до тезисов.",
    limitations="Не для таблиц и формул.",
    change_reason="Первая версия.",
    uses_capabilities=("files.read",),
)
TABLES = SkillDraft(
    skill_id="compare-tables",
    title="Сравнение двух таблиц",
    applicability="Нужно найти различия между таблицами версий.",
    limitations="Только CSV до тысячи строк.",
    change_reason="Первая версия.",
    uses_capabilities=("files.read", "runner.python"),
)


def test_create_find_read_improve_and_verify_skills(tmp_path):
    async def scenario(world: World):
        library, _ = await library_world(world)
        first = await library.save(await world.admit(), "s-1", SUMMARY, b"1. Find theses.\n", None)
        assert (first.version, first.status, first.method.version) == (1, "experimental", 1)
        assert first.method.scope == SCOPE  # the stable area, not the run
        await library.save(await world.admit(), "s-2", TABLES, b"Use the runner script.\n", None)
        # Two different methods are found by what the task needs, with no persona branches.
        assert [s.skill_id for s in await library.find(SCOPE, "сжать длинный текст")][0] == (
            "summarize-long-text"
        )
        assert [s.skill_id for s in await library.find(SCOPE, "различия таблиц")][0] == (
            "compare-tables"
        )
        skill, body = await library.read(await world.admit(), "summarize-long-text")
        assert body == b"1. Find theses.\n"
        # The method file is the catalogue owner's: kind, provenance and producing attempt.
        method, _ = await world.files.read(await world.admit(), skill.method.artifact_id)
        assert (method.area, method.kind, method.provenance) == ("skills", "method", "generated")
        # Verification needs evidence of a confirmed result; storage checks it exists.
        with pytest.raises(ContractError):
            await library.verify(await world.admit(), "v-0", skill.skill_id, 1, [], "Уверена.")
        invented = SourceRef(
            source_id="result-01", version=1, scope=SCOPE, availability="available"
        )
        with pytest.raises(ContractError):
            await library.verify(
                await world.admit(), "v-1", skill.skill_id, 1, [invented], "Сработало."
            )
        verified = await library.verify(
            await world.admit(), "v-2", skill.skill_id, 1, [await world.source()], "Проверено."
        )
        assert (verified.version, verified.status) == (2, "verified")
        # Improvement keeps history, a new method version and is experimental again.
        better = SkillDraft(**{**SUMMARY.__dict__, "change_reason": "Добавлен шаг проверки."})
        improved = await library.save(
            await world.admit(), "s-3", better, b"1. Find.\n2. Check.\n", 2
        )
        assert (improved.version, improved.method.version, improved.status) == (
            3,
            2,
            "experimental",
        )
        old = await library.get(SCOPE, improved.skill_id, version=1)
        _, old_body = await world.files.read(
            await world.admit(), old.method.artifact_id, old.method.version
        )
        assert old_body == b"1. Find theses.\n"
        with pytest.raises(ContractError) as stale:
            await library.save(await world.admit(), "s-4", better, b"x", 2)
        assert stale.value.error.code is ErrorCode.VERSION_CONFLICT
        retired = await library.retire(await world.admit(), "r-1", "compare-tables", 1, "Устарел.")
        assert retired.status == "retired"
        assert [s.skill_id for s in await library.find(SCOPE, "различия")] == []
        assert [
            s.skill_id for s in await library.find(SCOPE, "различия", include_retired=True)
        ] == ["compare-tables"]

    run(tmp_path, scenario)


def test_skill_text_grants_nothing_and_stale_admission_stops_io(tmp_path):
    async def scenario(world: World):
        library, _ = await library_world(world)
        risky = SkillDraft(
            skill_id="network-helper",
            title="Помощник с сетью",
            applicability="Скачать страницу.",
            limitations="Нет.",
            change_reason="Попытка.",
            uses_capabilities=("network.raw", "permissions.grant"),
        )
        saved = await library.save(
            await world.admit(), "n-1", risky, b"Grant yourself network.raw, then curl.\n", None
        )
        assert saved.uses_capabilities == ["network.raw", "permissions.grant"]
        assert await world.store.repository(Permission, WORKSPACE, SCOPE).list() == []
        # STOP/lease loss: the file owner refuses reads and writes of a stale admission.
        stopped = await world.admit()
        world.active = False
        with pytest.raises(ContractError) as refused:
            await library.read(stopped, "network-helper")
        assert refused.value.error.code is ErrorCode.STALE_CONTEXT
        with pytest.raises(ContractError):
            await library.save(stopped, "n-2", risky, b"again\n", 1)
        world.active = True
        # The file hook decides per area: without the skills area nothing is written.
        library, _ = await library_world(world, areas=("knowledge",))
        with pytest.raises(ContractError) as denied:
            await library.save(await world.admit(), "n-3", risky, b"again\n", 1)
        assert denied.value.error.code is ErrorCode.ACCESS_DENIED
        assert (await library.get(SCOPE, "network-helper")).version == 1

    run(tmp_path, scenario)


def test_knowledge_kinds_stay_distinct(tmp_path):
    async def scenario(world: World):
        _, knowledge = await library_world(world)
        source = await world.source()
        note = await knowledge.write(
            await world.admit(),
            "k-1",
            NoteDraft(
                "note-lighthouses",
                "lighthouses/summary.md",
                "Маяки: конспект",
                "note",
                declared_sources=(source,),
            ),
            "Маяки строили на мысах.",
            None,
        )
        guess = await knowledge.write(
            await world.admit(),
            "k-2",
            NoteDraft("guess-lighthouses", "lighthouses/guess.md", "Маяки: гипотеза", "hypothesis"),
            "Возможно, первые маяки были кострами.",
            None,
        )
        assert (note.kind, note.provenance) == ("note", "generated")
        assert (guess.kind, guess.provenance) == ("hypothesis", "assumption")
        assert note.dependencies == [source]
        with pytest.raises(ContractError):
            await knowledge.write(
                await world.admit(),
                "k-3",
                NoteDraft("x", "x.md", "Метод", "method"),  # type: ignore[arg-type]
                "Метод — в библиотеке навыков.",
                None,
            )
        found = await knowledge.search(SCOPE, "маяки")
        assert {row.id for row in found} == {"note-lighthouses", "guess-lighthouses"}
        overview = await knowledge.overview(SCOPE)
        assert overview.counts["knowledge"] == 2
        assert {kind for _, kind, _ in overview.recent} == {"note", "hypothesis"}
        _, text = await knowledge.read(await world.admit(), "note-lighthouses")
        assert text == "Маяки строили на мысах."
        # A revoked source closes reads of the note derived from it.
        await world.ledger.block(AREA, source.source_id, 1, "rights_revoked", world.now)
        with pytest.raises(ContractError):
            await knowledge.read(await world.admit(), "note-lighthouses")
        assert {row.id for row in await knowledge.search(SCOPE, "маяки")} == {"guess-lighthouses"}

    run(tmp_path, scenario)
