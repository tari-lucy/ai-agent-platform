"""M5-04: the twenty scenarios are well formed, cover the rubric and the doc22 scenes."""

import dataclasses

import pytest
from ai_agent_application.evaluation import CatalogueError, quality_catalogue
from ai_agent_application.evaluation.catalogue import validate
from ai_agent_application.evaluation.model import Fact, Forget, Say
from ai_agent_application.evaluation.rubric import CRITERIA
from ai_agent_application.laboratory import catalogue as lab_catalogue


def test_twenty_scenarios_twelve_reference_plus_eight_new():
    found = quality_catalogue()
    assert [s.scenario_id for s in found] == [f"Q-{n:02d}" for n in range(1, 21)]
    origins = [s.origin for s in found]
    assert origins.count("doc22") == 8 and origins.count("catalogue") == 4
    assert origins.count("new") == 8
    assert all(s.origin == "new" for s in found[12:])


def test_reference_scenarios_mirror_the_laboratory_catalogue():
    lab_ids = [s.scenario_id for s in lab_catalogue()]
    # STOP in the middle of a stream is not expressible as data steps (covered by M2-06/M5-03);
    # the quality form stops between messages instead
    lab_ids[lab_ids.index("stop-during-generation")] = "stop-then-resume"
    quality_slugs = [s.slug for s in quality_catalogue()[:12]]
    assert quality_slugs == lab_ids


def test_every_rubric_criterion_is_exercised_by_several_scenarios():
    found = quality_catalogue()
    for key, _ in CRITERIA:
        assert sum(key in s.focus for s in found) >= 2, key


def test_every_scenario_spans_several_sessions():
    # doc22: "20 многосессионных сценариев"
    assert all(s.sessions >= 2 for s in quality_catalogue())


def test_new_scenarios_do_not_repeat_reference_texts():
    found = quality_catalogue()
    reference = {step.text for s in found[:12] for step in s.steps if isinstance(step, Say)}
    new = {step.text for s in found[12:] for step in s.steps if isinstance(step, Say)}
    assert not reference & new


def test_text_is_synthetic_and_short():
    for s in quality_catalogue():
        for step in s.steps:
            if isinstance(step, Say):
                assert len(step.text) <= 160
                assert "@" not in step.text and "http" not in step.text


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda s: dataclasses.replace(s, focus=("charm",)), "UNKNOWN_CRITERION"),
        (lambda s: dataclasses.replace(s, steps=(Say("ghost", "привет"),)), "UNKNOWN_PERSON"),
        (
            lambda s: dataclasses.replace(
                s, steps=(Say("nika", "привет"), Forget("x"), Fact("a", key="x"))
            ),
            "FORGET_BEFORE",
        ),
        (lambda s: dataclasses.replace(s, steps=()), "NO_MESSAGES"),
        (lambda s: dataclasses.replace(s, expect="  "), "EXPECTATION_MISSING"),
    ],
)
def test_malformed_scenario_is_rejected(change, code):
    found = quality_catalogue()
    found[3] = change(found[3])
    with pytest.raises(CatalogueError, match=code):
        validate(found)


def test_wrong_count_and_duplicate_ids_are_rejected():
    found = quality_catalogue()
    with pytest.raises(CatalogueError, match="SCENARIO_COUNT"):
        validate(found[:19])
    found[5] = dataclasses.replace(found[5], scenario_id=found[4].scenario_id)
    with pytest.raises(CatalogueError, match="DUPLICATE"):
        validate(found)
