"""A basic personal-data redactor for trace content, until G1's sanitizer replaces it.

It masks what the mock scenarios contain (``pii_in_ticket``): email addresses, payment card numbers, US social
security numbers and phone numbers. Card candidates (13–19 digits) must pass the Luhn check, which keeps about 9 in
10 other long numbers (timestamps, ids); the rest are masked too. That false positive is the safe direction.
It is deliberately narrow: names and free-text details are not detected. G1 adds a real sanitizer (and
prompt-injection detection); ``configure_tracing(redact=...)`` takes any ``str -> str`` function.
"""

import re
from collections.abc import Callable

Redactor = Callable[[str], str]

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# A phone number needs separators or a +country code: a bare run of digits is far more often an id or a timestamp.
_PHONE = re.compile(r"(?<![\w-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]\d{3}[ .-]\d{4}\b")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group())
    return "[card]" if 13 <= len(digits) <= 19 and _luhn(digits) else match.group()


def basic_redact(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _CARD.sub(_card, text)
    text = _SSN.sub("[ssn]", text)
    return _PHONE.sub("[phone]", text)
