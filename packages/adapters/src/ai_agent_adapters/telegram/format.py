"""Her post text -> the small HTML subset Telegram renders (parse_mode=HTML).

She may write plain Markdown (**bold**, *italic*, `code`, ~~strike~~, [text](https://link),
"# heading", "- item") or the few HTML tags Telegram knows. Everything else is escaped, so a stray
``<`` or ``&`` shows as text and no tag the channel does not support can get through. Not exact
Markdown: just what a short post needs. A failed parse on Telegram's side falls back to plain text
(see ``TelegramBot``), so unbalanced markup never costs the post.
"""

import html
import re

_TAG = re.compile(
    r"(</?(?:b|strong|i|em|u|s|strike|del|code|pre|blockquote)>|<a href=\"https?://[^\"<>\s]+\">|</a>)",
    re.IGNORECASE,
)
_SAME = {"strong": "b", "em": "i", "strike": "s", "del": "s"}
_FENCE = re.compile(r"```[^\n`]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL)
_ITALIC_STAR = re.compile(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])")
_ITALIC_UNDER = re.compile(r"(?<![_\w])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![_\w])")
_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_BULLET = re.compile(r"^([ \t]*)[-*][ \t]+(?=\S)", re.MULTILINE)


def telegram_html(text: str) -> str:
    kept: list[str] = []

    def keep(value: str) -> str:
        kept.append(value)
        return f"\x00{len(kept) - 1}\x00"

    # code first, on the whole text: nothing inside it is a tag or markdown
    text = _FENCE.sub(lambda m: keep(f"<pre>{html.escape(m.group(1).rstrip())}</pre>"), text)
    text = _INLINE_CODE.sub(lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = _LINK.sub(
        lambda m: keep(
            f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>'
        ),
        text,
    )
    out: list[str] = []
    open_tags: list[str] = []
    for piece in _TAG.split(text):
        if not _TAG.fullmatch(piece):
            out.append(_markdown(piece))
            continue
        lowered = piece.lower()
        if lowered.startswith("<a "):
            out.append(f'<a href="{html.escape(piece[9:-2], quote=True)}">')
            open_tags.append("a")
        elif lowered.startswith("</"):
            name = _SAME.get(lowered[2:-1], lowered[2:-1])
            if name in open_tags:
                open_tags.remove(name)
                out.append(f"</{name}>")
            else:  # a closing tag nobody opened would break the whole message
                out.append(html.escape(piece, quote=False))
        else:
            name = _SAME.get(lowered[1:-1], lowered[1:-1])
            open_tags.append(name)
            out.append(f"<{name}>")
    out.extend(f"</{name}>" for name in reversed(open_tags))
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], "".join(out))


def _markdown(piece: str) -> str:
    piece = html.escape(piece, quote=False)
    piece = _HEADING.sub(r"<b>\1</b>", piece)
    piece = _BULLET.sub(r"\1• ", piece)
    piece = _BOLD.sub(r"<b>\2</b>", piece)
    piece = _STRIKE.sub(r"<s>\1</s>", piece)
    piece = _ITALIC_STAR.sub(r"<i>\1</i>", piece)
    return _ITALIC_UNDER.sub(r"<i>\1</i>", piece)
