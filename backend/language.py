"""Small language helpers shared by chat, dictation and voice. Languages are BCP-47 codes ("te", "pt-BR")."""

import unicodedata

# Languages usually written in a non-Latin script: Latin text in one of these is romanized
NON_LATIN = {
    "am", "ar", "as", "be", "bg", "bn", "el", "fa", "gu", "he", "hi", "hy", "ja", "ka", "kk", "km", "kn",
    "ko", "ky", "lo", "mk", "ml", "mn", "mr", "my", "ne", "or", "pa", "ps", "ru", "sd", "si", "sr", "ta",
    "te", "th", "uk", "ur", "yi", "zh",
}


def is_known(language: str | None) -> bool:
    return bool(language) and language.lower() not in ("und", "auto")


def base(language: str) -> str:
    """'pt-BR' -> 'pt'"""
    return language.split("-")[0].lower()


def latin_share(text: str) -> float:
    """Share of the letters in text that are Latin (0 when there are no letters)."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    latin = sum(1 for c in letters if unicodedata.name(c, "").startswith("LATIN"))
    return latin / len(letters)


def looks_romanized(text: str, language: str) -> bool:
    """Text in a non-Latin-script language that came back mostly in Latin letters."""
    return is_known(language) and base(language) in NON_LATIN and latin_share(text) > 0.5


def has_non_english_letters(text: str) -> bool:
    """True when text has letters outside plain ASCII (another script, or accented Latin)."""
    return any(c.isalpha() and not c.isascii() for c in text)
