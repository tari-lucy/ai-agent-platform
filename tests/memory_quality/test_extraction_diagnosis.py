"""An empty answer, a rejection and a refusal are told apart, without leaking content."""

import logging

from ai_agent_application.extraction.model import ExtractionReport
from ai_agent_application.memory import ClaimDraft
from ai_agent_contracts.memory import Claim
from ai_agent_contracts.states import Provenance
from extraction_support import MEMORY, answer, new_world, proposal, run

SAID = "Мне нравятся синие зонтики, давай встретимся у маяка в пятницу."
SECRET = "СЕКРЕТНАЯ-ФРАЗА-77"


def extract(tmp_path, scripts, text=SAID, **source):
    async def scenario():
        script = scripts[0]
        world = await new_world(tmp_path, [[] if callable(script) else script])
        ref = await world.source(text, **source)
        if callable(script):
            world.provider.scripts[0] = script(ref.source_id)
        report = await world.extractor.extract(world.scope, [ref.source_id])
        return world, ref, report

    return run(scenario())


def test_an_empty_model_answer_is_not_a_rejection(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    _, _, report = extract(tmp_path, [answer()])
    assert report.status == "nothing" and report.outcome == "model_returned_nothing"
    assert report.offered == 1 and report.proposed == 0 and not report.rejected
    assert "outcome=model_returned_nothing offered=1 proposed=0" in caplog.text


def test_a_paraphrased_quote_is_rejected_with_its_reason_and_nothing_is_stored(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    scripts = [
        lambda sid: answer(
            proposal(sid, "ей нравятся голубые зонтики", "Ей нравятся зонтики.", type_="reported"),
            proposal("wrong-source-id", SAID[:20], "Другое.", type_="reported"),
        )
    ]
    world, _, report = extract(tmp_path, scripts)
    assert report.outcome == "all_rejected" and not report.accepted
    assert sorted(r.reason for r in report.rejected) == ["QUOTE_NOT_IN_SOURCE", "UNKNOWN_SOURCE"]
    assert run(world.claims()) == []  # the rules are not loosened to obtain a claim
    line = next(r.message for r in caplog.records if "Extraction" in r.message)
    assert "QUOTE_NOT_IN_SOURCE:1" in line and "UNKNOWN_SOURCE:1" in line
    assert "зонтики" not in caplog.text and "маяка" not in caplog.text


def test_a_verbatim_quote_is_accepted_as_an_attributed_statement(tmp_path):
    scripts = [
        lambda sid: answer(
            proposal(
                sid,
                "давай встретимся у маяка в пятницу",
                "Собеседник договорился о встрече у маяка в пятницу.",
                type_="observed",
                subject="interlocutor",
            )
        )
    ]
    world, _, report = extract(tmp_path, scripts)
    assert report.outcome == "accepted" and report.accepted[0].knowledge_type == "reported"
    [claim] = run(world.claims())
    assert claim.content.startswith("По словам person-nika") and claim.confidence <= 0.6


def test_an_invalid_document_is_a_failure_with_a_fixed_code_not_a_rejection(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    scripts = [lambda sid: answer(proposal(sid, "давай встретимся", "X.", subject="собеседник"))]
    world, _, report = extract(tmp_path, scripts)
    assert report.status == "failed" and report.outcome == "failed" and report.failure
    assert run(world.claims()) == []
    assert "failure=" in caplog.text


def test_unavailable_sources_are_skipped_with_a_reason(tmp_path):
    _, _, report = extract(tmp_path, [answer()], api=False)
    assert report.outcome == "no_usable_sources" and report.offered == 0
    assert [s.reason for s in report.skipped] == ["API_TRANSFER_NOT_ALLOWED"]


def test_own_reply_never_confirms_a_fact_but_stays_citable(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [[]])
        own = await world.source(
            "Запомнила: тебе нравятся синие зонтики.",
            provenance=Provenance.MODEL_PROPOSAL,
            source_type="character-utterance",
            author="character",
        )
        said = await world.source(SAID)
        world.provider.scripts[0] = answer(
            proposal(
                own.source_id, "тебе нравятся синие зонтики", "Любит зонтики.", type_="observed"
            ),
            proposal(
                said.source_id,
                "Мне нравятся синие зонтики",
                "Собеседнику нравятся синие зонтики.",
                type_="observed",
                extra_evidence=[
                    {"source_id": own.source_id, "quote": "тебе нравятся синие зонтики"}
                ],
            ),
        )
        report = await world.extractor.extract(world.scope, [own.source_id, said.source_id])
        return report

    report = run(scenario())
    own_only, with_person = report.accepted
    assert (own_only.knowledge_type, own_only.independent_groups) == ("interpretation", 0)
    assert (with_person.knowledge_type, with_person.independent_groups) == ("reported", 1)
    assert all(a.knowledge_type not in ("observed", "canon") for a in report.accepted)


def test_a_contradiction_is_linked_to_the_old_claim_which_is_not_overwritten(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [[]])
        old_src = await world.source("Мне нравятся синие зонтики.")
        await world.memory.record_claim(
            world.command(),
            ClaimDraft(
                claim_id="claim-old",
                subject_id="interlocutor",
                content="По словам person-nika: любит синие зонтики.",
                knowledge_type="reported",
                sources=(old_src,),
                confidence=0.6,
            ),
        )
        # 25 unrelated claims would push the related one out of a plain first-20 list.
        for index in range(25):
            await world.memory.record_claim(
                world.command(),
                ClaimDraft(
                    claim_id=f"claim-z{index:02}",
                    subject_id="other",
                    content=f"Вымышленный посторонний факт номер {index}.",
                    knowledge_type="reported",
                    sources=(old_src,),
                    confidence=0.5,
                ),
            )
        new_src = await world.source("Теперь я не люблю синие зонтики.")
        world.provider.scripts[0] = answer(
            proposal(
                new_src.source_id,
                "не люблю синие зонтики",
                "Больше не любит синие зонтики.",
                type_="reported",
                contradicts=["claim-old"],
            )
        )
        report = await world.extractor.extract(world.scope, [new_src.source_id])
        return world, report

    world, report = run(scenario())
    assert "claim-old" in world.prompt()  # the related claim was shown, so it could be linked
    assert report.outcome == "accepted"
    claims = {c.id: c for c in run(world.claims())}
    new = next(c for c in claims.values() if "Больше не любит" in c.content)
    assert new.conflict_ids == ["claim-old"]
    assert claims["claim-old"].content.endswith("любит синие зонтики.")  # still there, unchanged


def test_diagnosis_is_fixed_vocabulary_only():
    report = ExtractionReport("nothing", offered=2, proposed=0)
    assert report.diagnosis() == (
        "outcome=model_returned_nothing offered=2 proposed=0 accepted=0 duplicate=0 "
        "rejected=- skipped=- attempts=0"
    )
    assert SECRET not in report.diagnosis() and Claim and MEMORY
