"""Which language a word names, however it is written.

The same language reaches this program in half a dozen spellings. The setting
says "German, English". Radarr's media information says ``ger/eng``, or
``deu/eng`` for the same two tracks written by a different muxer — the two
three-letter code lists disagree for about twenty languages. Another version
writes the names out in full. Compared as text, every German file in a German
library "lacked German", and a request for "en" was satisfied by a French
track, because "en" is inside "french".

So every spelling is reduced to one key — the two-letter code — before
anything is compared. A word that is not in the table is kept as it is,
lower case, so a language missing here still matches itself.
"""
from __future__ import annotations

import re

#: Every spelling per language: the two-letter code, both three-letter codes,
#: the English name, the name in the language itself where it is written in
#: Latin letters, and the German name — the interface is offered in German,
#: and "Französisch" is what a German speaker types into the setting.
_SPELLINGS: dict[str, tuple[str, ...]] = {
    "de": ("de", "ger", "deu", "german", "deutsch"),
    "en": ("en", "eng", "english", "englisch"),
    "fr": ("fr", "fre", "fra", "french", "francais", "français", "französisch"),
    "es": ("es", "spa", "spanish", "espanol", "español", "castellano",
           "spanisch"),
    "it": ("it", "ita", "italian", "italiano", "italienisch"),
    "nl": ("nl", "dut", "nld", "dutch", "nederlands", "flemish",
           "niederländisch"),
    "pl": ("pl", "pol", "polish", "polski", "polnisch"),
    "pt": ("pt", "por", "portuguese", "portugues", "português",
           "portugiesisch"),
    "ru": ("ru", "rus", "russian", "russisch"),
    "ja": ("ja", "jpn", "japanese", "japanisch"),
    "ko": ("ko", "kor", "korean", "koreanisch"),
    "zh": ("zh", "chi", "zho", "chinese", "mandarin", "cantonese",
           "chinesisch"),
    "cs": ("cs", "cze", "ces", "czech", "tschechisch"),
    "sk": ("sk", "slo", "slk", "slovak", "slowakisch"),
    "hu": ("hu", "hun", "hungarian", "ungarisch"),
    "tr": ("tr", "tur", "turkish", "türkisch"),
    "sv": ("sv", "swe", "swedish", "schwedisch"),
    "da": ("da", "dan", "danish", "dänisch"),
    "no": ("no", "nor", "nob", "nno", "norwegian", "norwegisch"),
    "fi": ("fi", "fin", "finnish", "finnisch"),
    "el": ("el", "gre", "ell", "greek", "griechisch"),
    "ro": ("ro", "rum", "ron", "romanian", "rumänisch"),
    "uk": ("uk", "ukr", "ukrainian", "ukrainisch"),
    "he": ("he", "heb", "hebrew", "hebräisch"),
    "ar": ("ar", "ara", "arabic", "arabisch"),
    "hi": ("hi", "hin", "hindi"),
    "th": ("th", "tha", "thai", "thailändisch"),
    "is": ("is", "ice", "isl", "icelandic", "isländisch"),
    "ms": ("ms", "may", "msa", "malay", "malaiisch"),
    "ca": ("ca", "cat", "catalan", "katalanisch"),
    "eu": ("eu", "baq", "eus", "basque", "baskisch"),
    "hr": ("hr", "hrv", "croatian", "kroatisch"),
    "sr": ("sr", "srp", "serbian", "serbisch"),
    "sl": ("sl", "slv", "slovenian", "slowenisch"),
    "bg": ("bg", "bul", "bulgarian", "bulgarisch"),
    "vi": ("vi", "vie", "vietnamese", "vietnamesisch"),
    "id": ("id", "ind", "indonesian", "indonesisch"),
}

_KEY = {spelling: code for code, spellings in _SPELLINGS.items()
        for spelling in spellings}

#: Tracks that say nothing about their language: undetermined, no speech at
#: all, not filled in.
UNKNOWN = frozenset({"und", "zxx", "mis", "mul", "unknown", "undetermined", ""})

_SPLIT = re.compile(r"[/,;|+&]|\s+-\s+|\s{2,}")


def key(word: str) -> str:
    """One language, as the key everything is compared by."""
    clean = (word or "").strip().lower()
    return _KEY.get(clean, clean)


def keys(text: str) -> list[str]:
    """Every language in a list such as ``ger/eng/eng``, as keys, in order."""
    out: list[str] = []
    for part in _SPLIT.split(text or ""):
        found = key(part)
        if found not in UNKNOWN and found not in out:
            out.append(found)
    return out
