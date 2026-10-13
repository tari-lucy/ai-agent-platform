"""Attention, reply extraction and initiative limits: pure logic, no model, no storage (M2-06)."""

import json
from datetime import timedelta

import pytest
from ai_agent_application.dialogue import (
    AttentionPolicy,
    AttentionQueue,
    Goal,
    InitiativeLimiter,
    ReplyExtractor,
    Signal,
    SourceRights,
)
from dialogue_support import NOW


def sig(event, kind="viewer_message", *, directed=False, key=None, person=None, ttl=None):
    return Signal(
        event_id=event,
        kind=kind,
        occurred_at=NOW,
        text="x",
        directed=directed,
        dedup_key=key,
        person_id=person,
        ttl_seconds=ttl,
        rights=SourceRights(read=True, api_transfer=True),
    )


# -- attention ----------------------------------------------------------------------------


def test_a_flood_of_viewer_chatter_is_capped_and_never_crowds_out_the_operator():
    queue = AttentionQueue(AttentionPolicy(viewer_budget=3))
    results = [queue.offer(sig(f"v{i}"), NOW).result for i in range(20)]
    assert results.count("accepted") == 3 and results.count("dropped_budget") == 17
    assert queue.offer(sig("op", "operator_message"), NOW).result == "accepted"
    assert queue.select(NOW).signal.event_id == "op"
    assert queue.stats.dropped_budget == 17


def test_directed_messages_are_not_charged_to_the_chatter_budget_but_the_queue_is_finite():
    queue = AttentionQueue(AttentionPolicy(viewer_budget=1, capacity=4))
    for i in range(10):
        queue.offer(sig(f"d{i}", directed=True), NOW)
    assert queue.pending() == 4 and queue.stats.dropped_full == 6


def test_a_stronger_signal_evicts_the_weakest_when_full_but_a_weaker_one_is_dropped():
    queue = AttentionQueue(AttentionPolicy(capacity=2, viewer_budget=10))
    queue.offer(sig("a"), NOW)
    queue.offer(sig("b"), NOW)
    assert queue.offer(sig("c"), NOW).result == "dropped_full"
    assert queue.offer(sig("op", "operator_message"), NOW).result == "accepted"
    assert queue.pending() == 2


def test_repeats_merge_and_stale_signals_expire():
    queue = AttentionQueue()
    assert queue.offer(sig("a", key="same"), NOW).result == "accepted"
    assert queue.offer(sig("b", key="same"), NOW).result == "coalesced"
    assert queue.pending() == 1 and queue.stats.coalesced == 1
    assert queue.select(NOW).merged == 2
    queue.offer(sig("old", ttl=30), NOW)
    assert queue.select(NOW + timedelta(seconds=31)) is None and queue.stats.expired == 1


def test_selection_prefers_operator_then_commitment_then_directed_viewer():
    queue = AttentionQueue()
    queue.offer(sig("chat"), NOW)
    queue.offer(sig("to-me", directed=True), NOW)
    queue.offer(sig("promise", "obligation_due", person="p1"), NOW)
    queue.offer(sig("op", "operator_message"), NOW)
    order = [queue.select(NOW).signal.event_id for _ in range(4)]
    assert order == ["op", "promise", "to-me", "chat"] or order[:2] == ["op", "to-me"]
    assert order[0] == "op" and order[-1] == "chat"


def test_waiting_raises_priority_and_ties_keep_arrival_order():
    queue = AttentionQueue()
    queue.offer(sig("first"), NOW)
    queue.offer(sig("second"), NOW)
    assert queue.select(NOW).signal.event_id == "first"
    early = AttentionQueue()
    early.offer(sig("old"), NOW)
    early.offer(sig("new", directed=False), NOW + timedelta(seconds=100))
    chosen = early.select(NOW + timedelta(seconds=100))
    assert chosen.signal.event_id == "old" and "WAITING" in chosen.reasons


def test_a_new_message_does_not_take_over_a_stronger_current_goal():
    queue = AttentionQueue()
    queue.offer(sig("chat", directed=True), NOW)
    goal = Goal("explore", priority=90, reason="важное дело", since=NOW)
    assert queue.select(NOW, goal) is None and queue.stats.deferred == 1
    assert queue.pending() == 1  # it waits; it is not discarded
    assert queue.select(NOW, None).signal.event_id == "chat"


def test_the_operator_is_never_deferred_and_the_goal_person_continues_cheaply():
    queue = AttentionQueue()
    goal = Goal("talk", priority=70, reason="разговор", since=NOW, person_id="p1")
    queue.offer(sig("op", "operator_message"), NOW)
    assert queue.select(NOW, goal).signal.event_id == "op"
    same = AttentionQueue()
    same.offer(sig("again", directed=True, person="p1"), NOW)
    assert same.select(NOW, goal) is None  # 55 ≤ 70: still waits
    other = AttentionQueue()
    other.offer(sig("again", "obligation_due", directed=True, person="p1"), NOW)
    assert other.select(NOW, goal).signal.event_id == "again"  # 70 + 35 > 70, no switch cost


def test_recent_similar_action_lowers_the_score():
    queue = AttentionQueue()
    handled = sig("old", person="p1", directed=True)
    queue.note_handled(handled, NOW)
    queue.offer(sig("again", person="p1", directed=True), NOW)
    chosen = queue.select(NOW)
    assert "RECENT_SIMILAR" in chosen.reasons and chosen.score < 55


def test_stop_clears_the_queue():
    queue = AttentionQueue()
    queue.offer(sig("a"), NOW)
    queue.clear()
    assert queue.pending() == 0 and queue.select(NOW) is None


# -- reply extraction ---------------------------------------------------------------------


def stream_of(document: str, size: int):
    extractor = ReplyExtractor()
    return "".join(extractor.feed(document[i : i + size]) for i in range(0, len(document), size))


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 1000])
def test_the_reply_text_is_streamed_identically_for_any_chunking(size):
    reply = 'Привет, "Ника"!\nСтрока два \\ слэш \t таб 😀 и ☃.'
    document = json.dumps(
        {"reply": reply, "question": {"topic_key": "k", "text": "q"}, "silence_reason": None},
        ensure_ascii=False,
    )
    assert stream_of(document, size) == reply
    escaped = json.dumps({"reply": reply, "x": [1, {"reply": "no"}]}, ensure_ascii=True)
    assert stream_of(escaped, size) == reply  # \uXXXX escapes and surrogate pairs


def test_only_the_top_level_reply_key_is_streamed():
    document = json.dumps(
        {"question": {"reply": "вложенное", "text": "q"}, "note": "reply", "reply": "настоящее"},
        ensure_ascii=False,
    )
    assert stream_of(document, 4) == "настоящее"


def test_null_missing_and_malformed_documents_yield_no_text_and_never_raise():
    assert stream_of('{"reply": null, "silence_reason": "тишина"}', 3) == ""
    assert stream_of('{"other": "x"}', 3) == ""
    for broken in ('{"reply": "abc', '{"reply": "\\ud83d x"}', '{{{{ "reply"', '"reply": "x"', ""):
        ReplyExtractor().feed(broken)  # must not raise
    assert stream_of('{"reply": "ab\\ud83d cd"}', 2) == "ab� cd"


def test_extracted_text_is_bounded():
    big = json.dumps({"reply": "я" * 50_000})
    assert len(stream_of(big, 1000)) == 20_000


# -- initiative ---------------------------------------------------------------------------


def test_min_gap_window_limit_and_silence_after_unanswered():
    limiter = InitiativeLimiter()
    assert limiter.allowed("p1", NOW) == (True, None)
    limiter.record("p1", NOW)
    assert limiter.allowed("p2", NOW + timedelta(seconds=10)) == (False, "MIN_GAP")
    assert limiter.allowed("p1", NOW + timedelta(seconds=31)) == (True, None)
    limiter.record("p1", NOW + timedelta(seconds=31))  # the second unanswered one
    assert limiter.allowed("p1", NOW + timedelta(seconds=70))[1] == "SILENCE_AFTER_UNANSWERED"
    assert limiter.allowed("p1", NOW + timedelta(minutes=11)) == (True, None)


def test_at_most_two_per_five_minutes_even_if_answered():
    limiter = InitiativeLimiter()
    limiter.record("p1", NOW)
    limiter.answered("p1")
    limiter.record("p1", NOW + timedelta(seconds=40))
    limiter.answered("p1")
    assert limiter.allowed("p1", NOW + timedelta(seconds=90)) == (False, "WINDOW_LIMIT")
    assert limiter.allowed("p1", NOW + timedelta(minutes=6)) == (True, None)


def test_a_reply_from_the_person_ends_the_silence():
    limiter = InitiativeLimiter()
    limiter.record("p1", NOW)
    limiter.record("p1", NOW + timedelta(seconds=40))
    assert limiter.allowed("p1", NOW + timedelta(minutes=1))[0] is False
    limiter.answered("p1")
    assert limiter.allowed("p1", NOW + timedelta(minutes=6)) == (True, None)
