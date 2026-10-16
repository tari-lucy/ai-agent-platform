"""One browser scenario on the real local host: long history, reading position, new reply while
reading, lost connection, reload, STOP and visibility-gated acknowledgement.

Mock provider only; no paid call. The history is fictional and its chronology is deliberately
scrambled against record ids.
"""

from pathlib import Path

from dialogue_daily_support import label, seed_history
from host_support import Server, free_port, init
from playwright.sync_api import expect

ROOT = Path(__file__).resolve().parents[2]
SEEDED = 230  # more than the automatic budget of 4 pages x 50 records
REPLY = "Вымышленный ответ технического mock-режима."
START = "START — запустить агента"
STOP = "STOP — остановить активного агента"
MIDDLE = (
    "() => { const e = document.querySelector('.timeline-scroll');"
    " e.scrollTop = e.scrollHeight / 2; }"
)
CHANGED = "document.dispatchEvent(new Event('visibilitychange')); }"
HIDE = (
    "() => { Object.defineProperty(document, 'visibilityState',"
    " { configurable: true, get: () => 'hidden' }); " + CHANGED
)
SHOW = "() => { delete document.visibilityState; " + CHANGED


def feed(page):
    return page.get_by_role("list", name="Лента диалога")


def texts(page):
    return [
        t.strip() for t in page.locator(".timeline .entry.incoming .plain-text").all_inner_texts()
    ]


def scroll_state(page):
    return page.evaluate(
        "() => { const e = document.querySelector('.timeline-scroll');"
        " return { top: e.scrollTop, height: e.scrollHeight, client: e.clientHeight }; }"
    )


def offset_of(page, text):
    """Distance of the entry with this text from the top of the scrolling feed."""
    return page.evaluate(
        """(text) => {
          const box = document.querySelector('.timeline-scroll').getBoundingClientRect();
          const node = [...document.querySelectorAll('.timeline .entry')].find(
            (n) => n.querySelector('.plain-text')?.textContent.trim() === text);
          return node ? node.getBoundingClientRect().top - box.top : null;
        }""",
        text,
    )


def test_long_history_reading_reply_connection_reload_stop_and_acknowledgement(page, tmp_path):
    root = tmp_path / "host"
    assert init(root).returncode == 0
    seed_history(root, SEEDED)
    password = (root / "secrets" / "operator-password.txt").read_text().strip()
    port, log = free_port(), tmp_path / "host.log"
    messages, acknowledgements = [], []

    def track(request):
        if request.method == "POST" and request.url.endswith("/api/v1/commands"):
            body = request.post_data_json
            if body.get("type") == "dialogue.message":
                messages.append(body["request_id"])
            if body.get("type") == "dialogue.acknowledge":
                acknowledgements.append(body["request_id"])

    page.on("request", track)
    with Server(root, port, log, panel_dir=ROOT / "apps/panel/dist") as server:
        page.goto(server.origin + "/#/dialogue")
        page.get_by_label("Пароль", exact=True).fill(password)
        page.get_by_role("button", name="Войти", exact=True).click()
        page.get_by_role("button", name=START, exact=True).click()
        expect(page.get_by_role("button", name=STOP, exact=True)).to_be_enabled()

        # 1. More than one page: honest about the unfinished part, then complete, in time order.
        status = page.locator("[data-history]")
        expect(status).to_contain_text("более ранние записи есть на сервере", timeout=20000)
        assert page.locator(".timeline .entry.incoming").count() == 200
        assert texts(page) == [label(rank) for rank in range(30, SEEDED)]
        expect(page.get_by_label("Связь диалога", exact=True)).to_contain_text(
            "События диалога подключены"
        )
        expect(feed(page)).to_be_visible()
        state = scroll_state(page)
        assert state["height"] - state["top"] - state["client"] <= 48  # starts at the newest
        page.evaluate("() => { document.querySelector('.timeline-scroll').scrollTop = 0; }")
        anchor = page.locator(".timeline .entry.incoming .plain-text").first.inner_text().strip()
        before = offset_of(page, anchor)
        page.get_by_role("button", name="Загрузить более ранние записи", exact=True).click()
        expect(status).to_contain_text("Вся переписка загружена", timeout=20000)
        shown = texts(page)
        assert shown == [label(rank) for rank in range(SEEDED)]  # no gap, no duplicate, by time
        # 2. Loading older records keeps the reader's place (the anchor entry did not move).
        after = offset_of(page, anchor)
        assert after is not None and abs(after - before) <= 3, (before, after)

        # 3. A reply arriving while the reader is up in the history does not move the view.
        page.evaluate(MIDDLE)
        reading = page.evaluate("() => document.querySelector('.timeline-scroll').scrollTop")
        page.get_by_label("Сообщение персонажу", exact=True).fill("Вымышленное новое сообщение.")
        page.get_by_role("button", name="Отправить", exact=True).click()
        jump = page.locator(".timeline-jump")
        expect(jump).to_be_visible(timeout=30000)
        expect(page.get_by_text(REPLY)).to_have_count(1, timeout=40000)
        assert (
            abs(
                page.evaluate("() => document.querySelector('.timeline-scroll').scrollTop")
                - reading
            )
            <= 3
        )
        assert acknowledgements == []  # the saved reply is below the fold: nothing was shown
        jump.click()
        expect(page.get_by_text("Приватная панель подтвердила получение.")).to_be_visible(
            timeout=20000
        )
        assert len(acknowledgements) == 1
        expect(jump).to_have_count(0)

        # 4. At the end the feed follows the next reply by itself; a hidden tab confirms nothing.
        page.evaluate(HIDE)
        page.get_by_label("Сообщение персонажу", exact=True).fill("Второе вымышленное сообщение.")
        page.get_by_role("button", name="Отправить", exact=True).click()
        expect(page.locator(".timeline .entry.reply")).to_have_count(2, timeout=30000)
        page.wait_for_timeout(600)
        assert len(acknowledgements) == 1
        page.evaluate(SHOW)
        expect(page.locator(".timeline .entry.reply").nth(1)).to_contain_text(
            "Приватная панель подтвердила получение.", timeout=20000
        )
        assert len(acknowledgements) == 2
        state = scroll_state(page)
        assert state["height"] - state["top"] - state["client"] <= 48  # followed to the end

        # 5. A reply lost on the wire: the outcome is checked with the SAME request id.
        sent = len(messages)
        held = []

        def lose_response(route):
            if route.request.post_data_json.get("type") == "dialogue.message":
                held.append(route.fetch())  # the core really receives and saves it
                route.abort()
            else:
                route.continue_()

        page.route("**/api/v1/commands", lose_response)
        page.get_by_label("Сообщение персонажу", exact=True).fill("Третье вымышленное сообщение.")
        page.get_by_role("button", name="Отправить", exact=True).click()
        expect(page.get_by_text("Результат отправки неизвестен")).to_be_visible()
        assert (
            page.get_by_label("Сообщение персонажу", exact=True).input_value()
            == "Третье вымышленное сообщение."
        )
        assert len(messages) == sent + 1  # no automatic second message
        page.unroute("**/api/v1/commands", lose_response)
        page.get_by_role("button", name="Проверить исход", exact=True).click()
        expect(page.get_by_text("Сообщение: ", exact=False).first).to_be_visible()
        assert len(messages) == sent + 2 and messages[-1] == messages[-2]  # same request id
        expect(
            page.locator(".timeline .entry.incoming .plain-text", has_text="Третье вымышленное")
        ).to_have_count(1, timeout=30000)

        # The replayed request produced exactly one more reply, not two.
        expect(page.locator(".timeline .entry.reply")).to_have_count(3, timeout=40000)
        page.wait_for_timeout(800)
        assert page.locator(".timeline .entry.reply").count() == 3
        # The new reply is at the end of the feed: shown, so confirmed once, before the line drops.
        expect(page.get_by_text("Приватная панель подтвердила получение.")).to_have_count(
            3, timeout=20000
        )
        assert len(acknowledgements) == 3

        # 6. Offline is stated and stale data is not passed off as current; coming back re-reads.
        page.context.set_offline(True)
        expect(page.get_by_text("Нет связи с сервером")).to_be_visible()
        expect(page.get_by_role("button", name=STOP, exact=True)).to_be_visible()
        posted = len(messages)
        page.context.set_offline(False)
        expect(page.get_by_text("Нет связи с сервером")).to_have_count(0, timeout=20000)
        assert len(messages) == posted  # reconnecting sends nothing

        # 7. Reload restores the history; START is not automatic; STOP stays available.
        page.reload()
        expect(feed(page)).to_be_visible(timeout=20000)
        expect(page.locator("[data-history]")).to_be_visible(timeout=20000)
        expect(page.get_by_role("button", name=STOP, exact=True)).to_be_enabled()
        # Reload reset the four-page budget: delivery history is incomplete, but exact
        # lookup still allows one acknowledgement of the next visible final answer.
        expect(page.locator("[data-history]")).to_contain_text("более ранние записи")
        page.get_by_label("Сообщение персонажу", exact=True).fill(
            "Четвёртое вымышленное сообщение."
        )
        page.get_by_role("button", name="Отправить", exact=True).click()
        expect(page.locator(".timeline .entry.reply")).to_have_count(4, timeout=30000)
        expect(page.get_by_text("Приватная панель подтвердила получение.")).to_have_count(
            4, timeout=20000
        )
        assert len(acknowledgements) == 4
        page.get_by_role("button", name=STOP, exact=True).click()
        expect(page.get_by_role("button", name="Отправить", exact=True)).to_be_disabled()
        expect(page.get_by_role("button", name=START, exact=True)).to_be_enabled(timeout=40000)
        page.reload()
        expect(page.get_by_role("button", name=START, exact=True)).to_be_enabled(timeout=20000)
        expect(page.get_by_role("button", name="Отправить", exact=True)).to_be_disabled()
        assert len(messages) == sent + 3
        page.goto("about:blank")
    assert password not in log.read_text(errors="replace")
