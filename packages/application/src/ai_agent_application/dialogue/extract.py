"""Incremental extraction of the ``reply`` string from a streaming JSON object.

The model answers with one JSON document. For the private panel the *text* of ``reply`` may be shown
while it is generated, but only as provisional text: the raw fragments are never parsed as a
command, and nothing but the final, fully validated document can cause an effect. This reader only
decodes the string value of the top-level ``reply`` key, correctly across arbitrary chunk
boundaries and escapes; it ignores every other key and never raises on malformed input.
"""

_SIMPLE = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_MAX_CHARS = 20_000


class ReplyExtractor:
    def __init__(self) -> None:
        self._depth = 0
        self._in_string = False
        self._escape = False
        self._hex: str | None = None
        self._high: int | None = None
        self._collecting_key = False
        self._key = ""
        self._current_key: str | None = None
        self._expect_value = False
        self._streaming = False
        self.done = False
        self.emitted = 0

    def feed(self, chunk: str) -> str:
        out: list[str] = []
        for char in chunk:
            if self._in_string:
                self._string_char(char, out)
            else:
                self._structure_char(char)
        text = "".join(out)
        room = _MAX_CHARS - self.emitted
        text = text[: max(0, room)]
        self.emitted += len(text)
        return text

    def _structure_char(self, char: str) -> None:
        if char == '"':
            self._in_string = True
            if self._depth == 1 and not self._expect_value:
                self._collecting_key, self._key = True, ""
            elif self._depth == 1 and self._expect_value:
                self._expect_value = False
                if self._current_key == "reply" and not self.done:
                    self._streaming = True
        elif char == ":" and self._depth == 1:
            self._expect_value = True
        elif char in "{[":
            self._depth += 1
            self._expect_value = False
        elif char in "}]":
            self._depth = max(0, self._depth - 1)
        elif char == "," and self._depth == 1:
            self._expect_value = False
            self._current_key = None
        elif not char.isspace() and self._depth == 1 and self._expect_value:
            self._expect_value = False  # a number / true / false / null value

    def _string_char(self, char: str, out: list[str]) -> None:
        if self._hex is not None:
            self._hex += char
            if len(self._hex) == 4:
                digits, self._hex = self._hex, None
                self._code_unit(digits, out)
            return
        if self._high is not None and char != "\\" and not (self._escape and char == "u"):
            self._high = None  # a high surrogate that no low surrogate follows
            self._emit("\ufffd", out)
        if self._escape:
            self._escape = False
            if char == "u":
                self._hex = ""
            else:
                self._emit(_SIMPLE.get(char, ""), out)
            return
        if char == "\\":
            self._escape = True
        elif char == '"':
            self._in_string = False
            if self._collecting_key:
                self._collecting_key, self._current_key = False, self._key
            elif self._streaming:
                self._streaming, self.done = False, True
        elif self._collecting_key:
            self._key += char
        else:
            self._emit(char, out)

    def _code_unit(self, digits: str, out: list[str]) -> None:
        try:
            unit = int(digits, 16)
        except ValueError:
            self._high = None
            self._emit("�", out)
            return
        if self._high is not None:
            high, self._high = self._high, None
            if 0xDC00 <= unit <= 0xDFFF:
                self._emit(chr(0x10000 + ((high - 0xD800) << 10) + (unit - 0xDC00)), out)
                return
            self._emit("�", out)
        if 0xD800 <= unit <= 0xDBFF:
            self._high = unit
        elif 0xDC00 <= unit <= 0xDFFF:
            self._emit("�", out)
        else:
            self._emit(chr(unit), out)

    def _emit(self, text: str, out: list[str]) -> None:
        if self._streaming and not self._collecting_key and text:
            out.append(text)
