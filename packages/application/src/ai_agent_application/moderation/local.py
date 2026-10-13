"""Local pre-checks that run before anything goes to the (external) moderator model.

A draft with a secret or private data is blocked here, so it is never transmitted to another
provider just to be told "no". These are floor checks, not a classifier.
"""

import re

_SECRETS = (
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b[ \t]*[:=][ \t]*\S{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer[ \t]+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\bhttps?://[^\s/@]+:[^\s/@]+@"),
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\s().-]?){10,15}(?!\d)")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")


def local_block(text: str) -> str | None:
    """Reason code if the text must not leave the machine, else None."""
    if _CONTROL.search(text):
        return "CONTROL_CHARACTER"
    if any(pattern.search(text) for pattern in _SECRETS):
        return "SECRET_LIKE"
    if _EMAIL.search(text):
        return "PRIVATE_DATA_EMAIL"
    if _PHONE.search(text):
        return "PRIVATE_DATA_PHONE"
    return None
