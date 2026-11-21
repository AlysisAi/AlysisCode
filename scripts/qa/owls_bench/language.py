"""Which writing system a reply is in, compared with the task's expected language.

Each letter's script is read from its Unicode character name (``unicodedata``), never from
word lists. A language tag maps to the scripts it is written in: its own script subtag when
it has one ("sr-Latn", "zh-Hant"), else the likely scripts below (the same defaults as the
Unicode CLDR likely-subtags data). When several languages share a script, as Spanish and
English share Latin, a matching script does not identify the language: the result says so
and leaves the language itself to the judge.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

MIN_LETTERS = 20

# ISO 15924 script subtags, by the name Unicode character names start with.
_SUBTAG_SCRIPTS: dict[str, tuple[str, ...]] = {
    "latn": ("Latin",),
    "grek": ("Greek",),
    "cyrl": ("Cyrillic",),
    "arab": ("Arabic",),
    "hebr": ("Hebrew",),
    "hans": ("Han",),
    "hant": ("Han",),
    "hani": ("Han",),
    "jpan": ("Han", "Kana"),
    "kore": ("Hangul", "Han"),
    "hang": ("Hangul",),
    "deva": ("Devanagari",),
    "beng": ("Bengali",),
    "taml": ("Tamil",),
    "telu": ("Telugu",),
    "gujr": ("Gujarati",),
    "guru": ("Gurmukhi",),
    "knda": ("Kannada",),
    "mlym": ("Malayalam",),
    "thai": ("Thai",),
    "geor": ("Georgian",),
    "armn": ("Armenian",),
    "ethi": ("Ethiopic",),
    "khmr": ("Khmer",),
    "laoo": ("Lao",),
    "mymr": ("Myanmar",),
    "sinh": ("Sinhala",),
}

# Likely scripts of primary language subtags (CLDR likely subtags).
_LIKELY_SCRIPTS: dict[str, tuple[str, ...]] = {
    **dict.fromkeys(
        (
            "af az ca cs cy da de en es et eu fi fil fo fr ga gl hr hu id is it lt lv ms mt "
            "nb nl nn no pl pt ro sk sl sq sv sw tl tr uz vi zu"
        ).split(),
        ("Latin",),
    ),
    "el": ("Greek",),
    **dict.fromkeys("be bg kk ky mk mn ru sr tg uk".split(), ("Cyrillic",)),
    **dict.fromkeys("ar fa ps ur".split(), ("Arabic",)),
    **dict.fromkeys("he yi".split(), ("Hebrew",)),
    "zh": ("Han",),
    "ja": ("Han", "Kana"),
    "ko": ("Hangul", "Han"),
    **dict.fromkeys("hi mr ne".split(), ("Devanagari",)),
    "bn": ("Bengali",),
    "ta": ("Tamil",),
    "te": ("Telugu",),
    "gu": ("Gujarati",),
    "pa": ("Gurmukhi",),
    "kn": ("Kannada",),
    "ml": ("Malayalam",),
    "th": ("Thai",),
    "ka": ("Georgian",),
    "hy": ("Armenian",),
    "am": ("Ethiopic",),
    "km": ("Khmer",),
    "lo": ("Lao",),
    "my": ("Myanmar",),
    "si": ("Sinhala",),
}

# Width forms are named after a qualifier ("FULLWIDTH LATIN CAPITAL LETTER A").
_QUALIFIERS = {"FULLWIDTH", "HALFWIDTH"}
_HIDDEN = re.compile(
    r"```.*?```|`[^`\n]*`|\]\([^)\s]*\)|[A-Za-z][A-Za-z0-9+.\-]{0,20}://\S+|\S+@\S+\.\w+",
    re.S,
)


def char_script(ch: str) -> str | None:
    """The script of one letter, named as its Unicode character name begins, or None."""
    if not unicodedata.category(ch).startswith("L"):
        return None
    words = unicodedata.name(ch, "").split()
    while words and words[0] in _QUALIFIERS:
        words = words[1:]
    if not words:
        return None
    head = words[0]
    if head == "CJK":
        return "Han"
    if head.startswith(("HIRAGANA", "KATAKANA")):
        return "Kana"
    return head.capitalize()


def script_counts(text: str) -> Counter:
    """Letters per script, leaving out web addresses, e-mail addresses and code.

    Compatibility forms (full-width letters, ligatures, the micro sign) are first folded to
    the letters they stand for (NFKC), so they count under their real script.
    """
    visible = unicodedata.normalize("NFKC", _HIDDEN.sub(" ", text or ""))
    return Counter(script for script in map(char_script, visible) if script)


def expected_scripts(language: str) -> tuple[str, ...]:
    """The scripts a BCP-47 language tag is written in; empty when not known."""
    subtags = [part.lower() for part in (language or "").replace("_", "-").split("-") if part]
    if not subtags:
        return ()
    for subtag in subtags[1:]:
        if len(subtag) == 4 and subtag in _SUBTAG_SCRIPTS:
            return _SUBTAG_SCRIPTS[subtag]
    return _LIKELY_SCRIPTS.get(subtags[0], ())


def script_identifies_language(language: str) -> bool:
    """True when no other known language is written in this language's scripts."""
    primary = (language or "").replace("_", "-").split("-")[0].lower()
    scripts = set(expected_scripts(language))
    if not scripts:
        return False
    return not any(
        other != primary and scripts & set(other_scripts)
        for other, other_scripts in _LIKELY_SCRIPTS.items()
    )


def check_reply_script(reply: str, language: str) -> dict:
    """Compare the reply's dominant script with the expected language's script.

    status: "pass" (dominant script matches), "fail" (another script dominates) or
    "unknown" (too few letters, or a language whose script is not known here).
    """
    counts = script_counts(reply)
    letters = sum(counts.values())
    expected = expected_scripts(language)
    shares = {script: round(count / letters, 3) for script, count in counts.most_common(5)}
    result = {
        "expected_language": language,
        "expected_scripts": list(expected),
        "letters": letters,
        "shares": shares,
        "dominant": counts.most_common(1)[0][0] if counts else None,
    }
    if not expected:
        return {
            **result,
            "status": "unknown",
            "language_identity": "left_to_judge",
            "detail": "No script is known for this language tag; the judge checks the language.",
        }
    if letters < MIN_LETTERS:
        return {
            **result,
            "status": "unknown",
            "language_identity": "left_to_judge",
            "detail": f"The reply has fewer than {MIN_LETTERS} letters to judge its script.",
        }
    matches = result["dominant"] in expected
    identifies = script_identifies_language(language)
    detail = (
        f"Mostly {result['dominant']} script, expected {' or '.join(expected)}."
        if matches
        else f"Mostly {result['dominant']} script, but {language} is written in "
        f"{' or '.join(expected)}."
    )
    if matches and not identifies:
        detail += (
            " Other languages share this script, so the judge decides whether the reply "
            "is in the expected language."
        )
    return {
        **result,
        "status": "pass" if matches else "fail",
        "language_identity": "confirmed_by_script" if matches and identifies else "left_to_judge",
        "detail": detail,
    }
