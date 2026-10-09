import re

def _title_prefixes():
    """Honorifics (Dr., Prof., ...) from config/nlu_lexicon.json."""

    import json
    import os

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "nlu_lexicon.json",
    )

    try:
        with open(path, encoding="utf-8") as fh:
            titles = json.load(fh).get("titles") or []
    except Exception:
        titles = []

    titles = titles or ["dr", "mr", "mrs", "ms", "prof"]

    return tuple(t.capitalize() + "." for t in titles)


def is_valid_teacher(name: str) -> bool:

    name = name.strip()

    if len(name) < 5:
        return False

    prefixes = _title_prefixes()

    # Remove prefix before validation
    for p in prefixes:
        if name.startswith(p):
            name = name[len(p):].strip()
            break

    # Reject abbreviations like AS, SK, XE1, X2, MnB...
    if re.fullmatch(r"[A-Z]{1,4}\d*", name):
        return False

    # Require at least two words after removing prefix
    if len(name.split()) < 2:
        return False

    return True