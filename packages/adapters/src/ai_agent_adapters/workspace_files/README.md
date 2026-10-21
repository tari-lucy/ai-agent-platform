# Managed filesystem AW-02

`DirectoryBlobs` хранит private immutable SHA-256 blobs, а `ManagedFiles`
(application/workspace_files) реализует принятый порт AW-00 через единственный
Owner.CATALOGUE. Имена/версии/связи/права находятся в существующей SQLite.

Trusted setup вызывает `DirectoryBlobs.prepare` только для нового каталога;
существующий root должен быть закрыт от agent/runner. Scope partition хэшируется,
blob names — только 64 lowercase hex. Raw root никогда не монтируется в sandbox.
`CatalogueUsage.counts` считает все metadata/revisions, включая скрытые записи,
под существующей SQLite write reservation. Все physical bytes, включая orphan
после прерванной записи, расходуют quota. AW-09 отвечает за их очистку/backup.

`ManagedFiles` требует FileLimits, реальный AttemptContext, FileReleaseGuard с
существующим DeletionLedger, mandatory authorize hook и актуальный WorkAdmission.
Source/file dependencies выводятся из записанных входов, не только из ответа
модели. Hook проверяет Permission агента и область; grants principal отдельно
ограничивают SQL. API/public transfer по умолчанию запрещены. Service list/search
доступны только trusted composition с собственным session/scope authorization.

`read_blob(root, digest, max_bytes=...)` — нижний физический примитив, без решения
о правах. POSIX использует pinned dir_fd/O_NOFOLLOW; Windows — directory handles
с FILE_LIST_DIRECTORY без delete sharing, проверяет reparse на handle. Файл
обычный, nlink=1, bounded read, digest и размер проверяются до возврата. Attribute-only
Windows handle не блокирует rename; не заменять handle IO на resolve/open.

Каталог и receipt публикуются после blob fsync в одной SQLite транзакции. Сбой
оставляет только недоступный orphan. POSIX directory fsync выполнен; Windows
power-cut durability metadata не заявляется. Physical IO ошибки application
преобразует в фиксированные коды без путей/текста. Не обходить owner release guard
через generic repository, особенно для state/access/TTL и старых версий.

Проверки: `python -m pytest -q tests/workplace_files`. На Windows basetemp — новый
уникальный путь собственного ignored runtime (pytest его очищает), нативным
link-тестам нужны права создавать синтетические ссылки. Linux/Windows skips
явные. Полные результаты/границы: docs/implementation-reports/AW-02.md.
