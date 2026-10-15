"""Actual production init/serve browser: TEST profile/GM edit and reviewed TEST->WORK transfer."""

from pathlib import Path

from host_support import Server, free_port, init
from playwright.sync_api import expect

ROOT = Path(__file__).resolve().parents[2]


def test_operator_controls_edit_test_settings_then_confirm_transfer(page, tmp_path):
    root = tmp_path / "host"
    done = init(root, "--operator-controls", "--test-scopes", "1")
    assert done.returncode == 0, done.stderr
    password = (root / "secrets" / "operator-password.txt").read_text().strip()
    with Server(root, free_port(), tmp_path / "log", panel_dir=ROOT / "apps/panel/dist") as server:
        page.goto(server.origin + "/#/persona")
        page.get_by_label("Пароль", exact=True).fill(password)
        page.get_by_role("button", name="Войти", exact=True).click()

        def select(space):
            page.get_by_role(
                "combobox", name="Просматриваемый персонаж и память", exact=True
            ).click()
            page.get_by_role("option", name=f"persona · ТЕСТ · {space}", exact=True).click()

        select("persona-settings-test")
        editor = page.get_by_label("Редактор профиля", exact=True)
        editor.get_by_label("Стиль", exact=True).fill("Вымышленный стиль из TEST.")
        editor.get_by_role("button", name="Проверить правку", exact=True).click()
        expect(editor.get_by_role("button", name="Применить версию", exact=True)).to_be_enabled()
        editor.get_by_role("button", name="Применить версию", exact=True).click()
        expect(page.get_by_label("Принятый профиль", exact=True)).to_contain_text(
            "Вымышленный стиль из TEST."
        )
        expect(page.get_by_role("table", name="Свойства личности")).to_contain_text("openness")
        page.get_by_role("link", name="Сюжет и развитие", exact=True).click()
        gm = page.get_by_label("Гейммастер", exact=True)
        expect(gm).to_contain_text("Тестовые настройки")
        gm.get_by_label("Интенсивность", exact=True).click()
        page.get_by_role("option", name="Высокая", exact=True).click()
        gm.get_by_role("button", name="Применить интенсивность", exact=True).click()
        expect(gm).to_contain_text("Текущая интенсивность: 0.8")
        page.get_by_role("link", name="Лаборатория", exact=True).click()
        form = page.get_by_label("Перенос настроек", exact=True)
        form.get_by_role("button", name="Предпросмотр переноса", exact=True).click()
        preview = form.get_by_label("Предпросмотр переноса", exact=True)
        expect(preview).to_contain_text("0.8")
        apply = form.get_by_role("button", name="Перенести настройки", exact=True)
        expect(apply).to_be_disabled()
        form.get_by_label(
            "Подтверждаю перенос выбранных настроек в рабочую область", exact=True
        ).check()
        apply.click()
        expect(form.get_by_label("Состояние переноса", exact=True)).to_contain_text(
            "Перенос завершён"
        )
        page.get_by_role("combobox", name="Просматриваемый персонаж и память", exact=True).click()
        page.get_by_role(
            "option", name="persona · Рабочая память · persona-work", exact=True
        ).click()
        page.get_by_role("link", name="Персонаж", exact=True).click()
        # Default transfer selects GM intensity only: profile stays WORK until explicitly selected.
        expect(page.get_by_label("Принятый профиль", exact=True)).not_to_contain_text(
            "Вымышленный стиль из TEST."
        )
        page.reload()
        expect(page.get_by_label("Принятый профиль", exact=True)).not_to_contain_text(
            "Вымышленный стиль из TEST."
        )
        page.goto("about:blank")
