"""The active time is saved once a minute, not every tick: each save is a new version of the game master's
state plus an event and an operation, and a week of saving every five seconds made a 330 MB database."""

from ai_agent_core.m3_composition.service import TIME_SAVE_EVERY, Every


def test_the_first_question_is_a_yes_and_then_once_per_period():
    every = Every(60)
    assert every.due(0.0) is True
    assert [every.due(t) for t in (5, 30, 59.9)] == [False, False, False]
    assert every.due(60.0) is True
    assert every.due(61) is False and every.due(120.0) is True


def test_a_runtime_tick_every_five_seconds_saves_the_time_twelve_times_less_often():
    every, saves = Every(TIME_SAVE_EVERY), 0
    for tick in range(0, 3600, 5):  # an hour of ticks, as the runtime makes them
        saves += every.due(float(tick))
    assert saves == 60 and TIME_SAVE_EVERY >= 30                      # was 720 an hour


def test_a_clock_that_jumps_back_is_not_a_reason_to_save_more():
    every = Every(60)
    assert every.due(1000.0) and not every.due(10.0) and not every.due(1059.0) and every.due(1060.0)
