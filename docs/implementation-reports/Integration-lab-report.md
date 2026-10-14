Актуализация координатора 2026-10-15: download button, canonical catalogue/start,
report ACL и durable reservation подготовленного TEST pool подключены:
[Integration-laboratory-panel](Integration-laboratory-panel.md).
Ниже — предыдущая приёмка; новый host provisioning/events/stop/transfer остаются отдельно.

# Integration-lab-report

Дата: 2026-10-15. Исполнитель: Codex, отдельная codex/integration-lab-report,
base d97c5ee. Техническая приёмка ограниченного private/mock download закрыта.
Feature формы M4-06 и весь M4/M5 этим отчётом не принимаются.

## Результат

Ранее сохранялся полный ScenarioReport, но HTTP отдавал только metadata.
Теперь GET /api/v1/laboratory-reports/{report_id}/content возвращает точные
JSON bytes из существующего Reports, без новой БД/owner/копии отчёта.
Host передаёт Reports явно; default deploy остаётся закрытым.

Сохранены operator cookie/Host/Origin/session и partition gates. Только TEST;
для LaboratoryReport, Artifact и Source требуются действующие grants/ACL и
api_transfer. Закрепляется вся ArtifactRef, включая version/checksum/size;
проверяются TTL, доступность, отсутствие symlink, фактический byte count/SHA-256.
Чтение bounded закреплённым размером, максимум 2 000 000 bytes. После IO повторно
проверяются Artifact/Source, report и session. Удаление/отзыв закрывают чтение
даже при сохранённом JSON файле. Выдача после STOP возможна только по
сохранившимся read rights, без нового job/модели.

Response: application/json, фиксированное attachment имя, quoted checksum ETag,
no-store/nosniff. Redirect/range/cache replay и произвольный pathname/URL
не поддерживаются; сырые file/provider exceptions в ответ не попадают.

Reports.api_transfer_allowed по умолчанию False. Доверенный host может явно
разрешить API transfer новых mock Source и отдельно выдать минимальные
scoped grants/record ACL. Endpoint не вызывает provision, не создаёт права
и не переоткрывает старые закрытые Source. Пользовательская БД не изменялась.

## Порт для второго агента

В существующем PanelApi добавлен laboratoryReportContent(scope, report, signal):
принимает текущий LaboratoryReport, возвращает Blob после проверки scope,
ArtifactRef/лимита/ETag/SHA-256 и mock/scenario ID/version. Смена auth generation/
abort отбрасывает ожидающий ответ. Нет второго fetch client и автоматических
повторов. JSON projection limit 1 MiB сохранён; отдельный download bounded
размером reference до 2 000 000 bytes. OpenAPI обновлён из canonical exporter.

Владелец M4-06 подключает кнопку через этот метод к выбранному report в TEST
scope; object URL освобождается после использования. Не формировать прямые
file paths или remote URLs. Пока runtime не имеет явных ACL/Reports, показывать
честную недоступность. Автовыделение TEST scopes остаётся отдельным запросом.

## Проверки

- Windows: начальные 12 новых ASGI/SQLite cases passed, 24.02 s.
- Linux final affected: access + contracts + Reports/settings + новый download,
  184 passed, 53.07 s. Cached Python3.14 image, network none, CPU2/RAM1536MiB,
  read-only source mounts; без реальных env/данных и второй рабочей памяти.
- После Linux gate добавлен только regression закреплённой ArtifactRef.version:
  Windows этот case и source-delete case passed, 2 passed / 13 deselected,
  5.88 s. Повтор полного backend или Linux набора не выполнялся.
- Проверены положительные точные bytes/заголовки и отказы: отсутствие auth,
  foreign Origin/scope, неизвестный ID, лишние query fields, каждый owner grant,
  ACL отчёта/artifact/source, default Source API false, отсутствие Reports,
  повреждение/oversize/missing file, TTL, source deletion, revoke/logout во
  время IO, stale ArtifactRef version.
- Pure TS: 152 passed (146 существующих + 6 download), включая report >1 MiB,
  overflow/truncation/checksum/ETag/media mismatch, foreign scope, неверную
  ссылку/mock/scenario и clear-auth во время body IO.
- TypeScript typecheck, ESLint и production build passed.
- Ruff, mypy171, import boundaries и canonical export check passed.
- Diff/состав файлов проверены, секреты и личные данные в изменениях не найдены.

Первый client прогон выявил неверный limit в служебной обёртке validation;
исправлено, шесть новых client cases и затем весь быстрый TS набор passed.
Первоначальные Ruff format и ESLint prefer-const замечания также устранены.
Failures не скрывались удалением checks или ослаблением прав.

## Не проверено и оставшаяся работа

- Feature кнопка/браузерный download не подключались: это зона второго агента.
  Здесь настоящие ASGI/SQLite и shared client, без новой UI acceptance.
- Полный backend не повторялся: использован affected gate по договорённости
  об экономных проверках. Новый lab scenario/LLM не запускался повторно ради
  download; применён синтетический сохранённый report через настоящих owners.
- Live LLM, пользовательский профиль/БД/ключи, VPS venv и внешние каналы не
  использовались; моделей/зависимостей не скачивали.
- Host setup, выделение lab TEST scope, settings transfer, development decisions/
  rollback, memory plans/export и usage aggregates остаются своими задачами.
- Windows backup ACL/operations blocker не изменён.
- Linux дал 6 известных legacy WebSocket warnings; Vite — прежнее предупреждение
  bundle >500kB. Изменение transport/bundle не входит в эту приёмку.

Migrations, dependencies/lockfiles, Compose, реестр решений, business M2 и
feature формы другого агента не менялись.
