"""Deterministic synthetic corpus; annotations are authored before measurement."""

import json
from pathlib import Path

FIXTURE = Path(__file__).parent
NOW = 100
SEED = 2405


def load_queries():
    return json.loads((FIXTURE / "queries.json").read_text(encoding="utf-8"))


def corpus(size):
    rows = json.loads((FIXTURE / "sources.json").read_text(encoding="utf-8"))
    assert size >= len(rows)
    actions = (
        "проверил крепление",
        "обсудил план",
        "нарисовал схему",
        "почистил поверхность",
        "перенёс материалы",
        "сравнил размеры",
        "записал наблюдение",
        "попросил совет",
        "проверил расписание",
        "заменил деталь",
        "отложил осмотр",
        "подтвердил получение",
        "выбрал образец",
        "измерил влажность",
        "исправил заметку",
        "отказался от поездки",
        "вернул предмет",
        "обещал помощь",
        "перечитал письмо",
        "встретил знакомого",
    )
    objects = (
        "мост",
        "фонарь",
        "компас",
        "лодка",
        "струна",
        "цветочный горшок",
        "карта",
        "чертёж",
        "часы",
        "линза",
        "атлас",
        "печь",
        "семена",
        "колокол",
        "минерал",
        "краска",
        "ключ",
        "насос",
        "канат",
        "лестница",
    )
    places = (
        "мастерская",
        "теплица",
        "библиотека",
        "переправа",
        "площадь",
        "зал",
        "лесной лагерь",
        "берег",
        "смотровая башня",
        "рыночный павильон",
        "склад",
        "читальня",
        "восточный двор",
        "старая пристань",
        "сад",
        "музыкальная комната",
        "северная тропа",
        "лаборатория",
        "вестибюль",
        "музей",
        "мельница",
        "сельская дорога",
        "сушильня",
        "поляна",
        "крытая галерея",
    )
    nicks = ("Блик", "Лира", "Рун", "Нокс", "Эхо", "Мира")
    # 20 * 20 * 25 combinations, permuted by a coprime multiplier.
    for number in range(size - len(rows)):
        variant = (number * 7919 + SEED) % 10000
        action, obj, place = variant % 20, variant // 20 % 20, variant // 400
        person = number % 6
        nick = nicks[person]
        rowid = len(rows) + 1
        rows.append(
            {
                "id": rowid,
                "source_id": f"background-{number:05d}",
                "title": f"Лабораторная заметка {number:05d}",
                "text": f"{nick}: {actions[action]}. Объект: {objects[obj]}. "
                f"Место: {places[place]}. Запись номер {number:05d}, "
                f"день {number % 28 + 1} августа. Итог относится только к этому осмотру.",
                "scope": "test" if number % 8 == 0 else "work",
                "persona": "other" if number % 8 == 1 else "iskra",
                "actor": f"person-{person + 1:02d}",
                "nick": nick,
                "date": f"2026-08-{number % 28 + 1:02d}",
                "version": 1,
                "index_version": 1,
                "deleted": number % 8 == 2,
                "expires": 99 if number % 8 == 3 else 200,
                "permission": True,
                "kind": "episode",
                "status": "",
            }
        )
    assert len(rows) == size
    assert len({r["id"] for r in rows}) == size
    assert len({r["text"] for r in rows[60:]}) == size - 60
    return rows
