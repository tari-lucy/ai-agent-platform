"""Offline operator commands for the book of people (doc37, D94).

The operator decides who is the same person, undoes a wrong join, and erases a person on their
request. The platform may be running: the book is one SQLite file with short transactions.
A person is named by its id (``p-...``) or by an account (``platform:account``).
"""

import json
from pathlib import Path

from ai_agent_core.m3_composition.harness_people import PeopleBook, PeopleError

from .layout import HostError, paths

ACTIONS = ("list", "show", "link", "split", "erase", "suspicions", "audit")


def _ref(book: PeopleBook, ref: str) -> str:
    """A person id from ``p-...`` or ``platform:account``."""
    if ref.startswith("p-"):
        return ref
    platform, _, account = ref.partition(":")
    person = book.lookup(platform, account) if account else None
    if person is None:
        raise HostError("PERSON_UNKNOWN")
    return person


def run(root: Path, action: str, args: list[str], *, yes: bool) -> int:
    host = paths(root)
    database = host.root / "data" / "harness" / "people.sqlite"
    if not database.is_file():
        raise HostError("PEOPLE_BOOK_MISSING")  # created by the first START with the harness on
    book = PeopleBook(database)
    try:
        if action == "list":
            for row in book.people():
                print(f"{row['person_id']}  {row['name']}  {', '.join(row['accounts'])}")
        elif action == "show" and len(args) == 1:
            print(
                json.dumps(
                    book.card(_ref(book, args[0]), public=False), ensure_ascii=False, indent=2
                )
            )
        elif action == "link" and len(args) == 2:
            winner = book.merge(_ref(book, args[0]), _ref(book, args[1]), by="operator")
            print(f"joined into {winner}")
        elif action == "split" and len(args) == 1:
            platform, _, account = args[0].partition(":")
            print(f"now {book.split(platform, account, by='operator')}")
        elif action == "erase" and len(args) == 1:
            if not yes:
                raise HostError("ERASE_NEEDS_YES")  # deleting a person cannot be undone
            print(f"erased, {book.erase(_ref(book, args[0]), by='operator')} account(s) closed")
        elif action == "suspicions" and not args:
            for row in book.suspicions():
                print(
                    f"{row['a_platform']}:{row['a_account']}  ~  "
                    f"{row['b_platform']}:{row['b_account']}  {row['basis']}"
                )
        elif action == "audit" and not args:
            for row in book.audit():
                print(row["id"], row["action"], row["detail"])
        else:
            raise HostError("INVALID_OPTION")
    except PeopleError as error:
        raise HostError("PERSON_UNKNOWN") from error
    return 0
