import re as _re

MOJIBAKE_THRESHOLD = 0.9

def looks_like_mojibake(s: str) -> bool:
    """Detect cp1251 bytes decoded as latin-1 (e.g. Àêóñòè÷ instead of Акусти).
    Only flags strings where >=90% of alphabetic chars are non-ASCII latin-1,
    to avoid false positives on words like Motörhead or Prélude.
    """
    if not s:
        return False
    try:
        raw = s.encode("latin-1")
    except UnicodeEncodeError:
        return False
    try:
        fixed = raw.decode("cp1251")
    except UnicodeDecodeError:
        return False

    alpha = [c for c in s if c.isalpha()]
    if not alpha:
        return False
    suspicious = [c for c in alpha if ord(c) >= 0x80]
    if len(suspicious) / len(alpha) < MOJIBAKE_THRESHOLD:
        return False

    has_cyrillic      = any("Ѐ" <= c <= "ӿ" for c in fixed)
    orig_has_cyrillic = any("Ѐ" <= c <= "ӿ" for c in s)
    return has_cyrillic and not orig_has_cyrillic

def fix_mojibake(s: str) -> str:
    return s.encode("latin-1").decode("cp1251")

def _is_wrongendian_ascii(c: str) -> bool:
    """U+XX00 where XX is printable ASCII — all-ASCII UTF-16LE read as BE."""
    code = ord(c)
    return (code & 0xFF) == 0x00 and 0x20 < (code >> 8) <= 0x7E

def _is_wrongendian_cyrillic(c: str) -> bool:
    """U+XX04 where 0x04XX is a Cyrillic codepoint — Cyrillic UTF-16LE read as BE."""
    code = ord(c)
    if (code & 0xFF) != 0x04:
        return False
    swapped = ((code & 0xFF) << 8) | (code >> 8)
    return 0x0400 <= swapped <= 0x04FF

def _is_wrongendian(c: str) -> bool:
    return _is_wrongendian_ascii(c) or _is_wrongendian_cyrillic(c)

def looks_like_utf16le_as_be(s: str) -> bool:
    """Detect UTF-16LE text read as UTF-16BE."""
    if not s:
        return False
    chars = [c for c in s if c != " "]
    if not chars:
        return False
    if any(_is_wrongendian_cyrillic(c) for c in chars):
        return True
    hits = sum(1 for c in chars if _is_wrongendian_ascii(c))
    return hits / len(chars) >= 0.8

def fix_utf16le_as_be(s: str) -> str:
    result = []
    for c in s:
        if _is_wrongendian(c):
            code = ord(c)
            result.append(chr(((code & 0xFF) << 8) | (code >> 8)))
        else:
            result.append(c)
    return "".join(result)

def check_encoding(tags: dict) -> dict:
    """Return {field: fixed_value} for encoding issues (mojibake or UTF-16LE-as-BE)."""
    fixes = {}
    for field, value in tags.items():
        if not value:
            continue
        if looks_like_utf16le_as_be(value):
            fixes[field] = fix_utf16le_as_be(value)
        elif looks_like_mojibake(value):
            fixes[field] = fix_mojibake(value)
    return fixes
