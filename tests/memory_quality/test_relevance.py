"""Russian-tolerant selection and bounded excerpts of long memory."""

from ai_agent_application.extraction.relevance import related, score, words

from ai_agent_core.m3_composition.long_term import excerpt


def test_endings_and_yo_do_not_hide_a_related_word():
    assert related("зонтиками", "зонтики") and related("предпочитает", "предпочитаю")
    assert related("пришёл".replace("ё", "е"), "пришел")
    assert not related("ника", "никто") and not related("замок", "зонтик")
    query = words("Какие зонтиками любит Ника?")
    assert score("Ника любит синие зонтики.", query) == 3
    assert score("Погода сегодня хорошая.", query) == 0


def test_a_long_summary_is_shortened_around_the_relevant_sentence_and_marked():
    filler = " ".join(f"Обсуждали вымышленную тему номер {i}." for i in range(40))
    text = filler + " Договорились встретиться у маяка в пятницу."
    query = words("Где мы договорились встретиться?")
    shown, cut = excerpt(text, query, 120)
    assert cut and "у маяка в пятницу" in shown and shown.startswith("… ")
    assert len(shown) <= 120 * 3 + 2
    head, head_cut = excerpt(text, words("Совсем другое"), 120)
    assert head_cut and head.startswith("Обсуждали") and head.endswith("…")
    short, short_cut = excerpt("Коротко.", query, 120)
    assert (short, short_cut) == ("Коротко.", False)
