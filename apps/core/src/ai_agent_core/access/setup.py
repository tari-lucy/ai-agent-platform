"""Explicit human-run setup; creates a separate ignored binding, never reads .env."""

import getpass
import os
import secrets
from pathlib import Path

from pydantic import SecretStr

from .sessions import HASH, password_hash


def write_binding(path: Path, verifier: SecretStr) -> None:
    """Exclusive creation; no overwrite, no inspection of an existing binding."""
    value = verifier.get_secret_value()
    if HASH.fullmatch(value) is None:
        raise ValueError("PASSWORD_HASH")
    # Single quotes keep scrypt '$' characters literal in Compose/dotenv parsers.
    content = (
        "AI_PLATFORM_OPERATOR_PASSWORD_HASH='" + value + "'\n"
        "AI_PLATFORM_WORKER_SERVICE_TOKEN='" + secrets.token_hex(32) + "'\n"
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def main() -> None:
    try:
        password = getpass.getpass("Пароль локального оператора: ")
        confirm = getpass.getpass("Повторите пароль: ")
        if password != confirm or len(password) < 12 or len(password.encode("utf-8")) > 1024:
            raise ValueError("PASSWORD")
        write_binding(Path(".env.access"), password_hash(SecretStr(password)))
    except OSError, ValueError, EOFError:
        print("Привязка не создана: проверьте пароль и отсутствие файла .env.access.")
        raise SystemExit(78) from None
    print("Создан .env.access. Содержимое привязки не выводится.")


if __name__ == "__main__":
    main()
