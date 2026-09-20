"""
Structural helpers for faculty names.

Nothing in this module knows any real teacher, subject, class or room.
It only applies *shape* rules that hold for any timetable:

* honorific titles ("Dr.", "Mr.", ...) come from config/nlu_lexicon.json;
* a "placeholder / code" entry is a name with no real word in it
  (e.g. an initials-only code such as "AS", or a name containing a digit
  such as "XE2") - these are not people who can be substituted or
  reported as free;
* a "composite" entry is a name that is exactly the concatenation of two
  other teacher names already present in the data (a parser artefact for
  co-taught slots).
"""

import json
import os
import re

_CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    os.pardir,
    "config",
)

_LEXICON_CACHE = None


def load_lexicon(path=None):
    """Load the language lexicon (cached for the default path)."""

    global _LEXICON_CACHE

    if path is not None:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    if _LEXICON_CACHE is None:

        default_path = os.path.join(
            _CONFIG_DIR,
            "nlu_lexicon.json",
        )

        with open(default_path, "r", encoding="utf-8") as handle:
            _LEXICON_CACHE = json.load(handle)

    return _LEXICON_CACHE


def alnum_key(text):
    """Lower-case letters and digits only - a spelling-insensitive key."""

    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _titles(lexicon=None):

    lexicon = lexicon or load_lexicon()

    return set(lexicon.get("titles", []))


def name_tokens(name, lexicon=None):
    """
    Lower-case alphanumeric tokens of a name with leading honorifics
    removed.  "Dr.Ashish Nayyar" -> ["ashish", "nayyar"].
    """

    titles = _titles(lexicon)

    tokens = re.findall(
        r"[A-Za-z0-9]+",
        str(name or "").replace(".", " "),
    )

    tokens = [t.lower() for t in tokens]

    while tokens and tokens[0] in titles:
        tokens = tokens[1:]

    return tokens


def strip_title(name, lexicon=None):
    """Name without honorifics, original letter case kept."""

    titles = _titles(lexicon)

    tokens = re.findall(
        r"[A-Za-z0-9]+",
        str(name or "").replace(".", " "),
    )

    while tokens and tokens[0].lower() in titles:
        tokens = tokens[1:]

    return " ".join(tokens)


def name_key(name, lexicon=None):
    """Title-less, spelling-insensitive identity key of a name."""

    return "".join(name_tokens(name, lexicon))


def is_placeholder_name(name, lexicon=None):
    """
    True when the entry is not a real person's name:
    it has no word of at least ``min_real_token_len`` letters, or it
    contains a digit (codes like "XE2", "X7").
    """

    lexicon = lexicon or load_lexicon()

    rules = lexicon.get("placeholder_rules", {})

    min_len = int(rules.get("min_real_token_len", 4))
    digits_bad = bool(rules.get("digits_mark_placeholder", True))

    tokens = name_tokens(name, lexicon)

    if not tokens:
        return True

    if digits_bad and any(re.search(r"\d", t) for t in tokens):
        return True

    return not any(
        len(re.sub(r"[^a-z]", "", t)) >= min_len
        for t in tokens
    )


def find_composites(names, lexicon=None):
    """
    Detect names that are the concatenation of two other names.

    Returns {composite_name: (name_a, name_b)}.
    """

    by_key = {}

    for name in names:
        by_key.setdefault(name_key(name, lexicon), name)

    composites = {}

    for name in names:

        tokens = name_tokens(name, lexicon)

        for split in range(1, len(tokens)):

            left = "".join(tokens[:split])
            right = "".join(tokens[split:])

            if (
                left in by_key
                and right in by_key
                and by_key[left] != name
                and by_key[right] != name
            ):
                composites[name] = (by_key[left], by_key[right])
                break

    return composites