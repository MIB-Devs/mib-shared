"""One rule that turns a title into a URL slug (mib-shared#51).

Ingestion and the regulation admin both write records, so the rule lives here:
two copies would give one title two slugs. Uniqueness is the caller's job.
"""
import re
import unicodedata

DEFAULT_FALLBACK = "dokumen"
DEFAULT_MAX_LENGTH = 120

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    """Fold to ASCII (NFKD, drop marks), lower-case, one ``-`` per run of other characters."""
    folded = unicodedata.normalize("NFKD", text)
    ascii_text = folded.encode("ascii", "ignore").decode("ascii").lower()
    return _NON_ALNUM.sub("-", ascii_text).strip("-")


def _cut(slug: str, max_length: int) -> str:
    """Cut on the last ``-`` within the limit; hard-cut when the first word is longer."""
    if len(slug) <= max_length:
        return slug
    head = slug[: max_length + 1]
    boundary = head.rfind("-")
    cut = head[:boundary] if boundary > 0 else slug[:max_length]
    return cut.strip("-")


def make_slug(text: str, *, fallback: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Return the slug of ``text``, or of ``fallback``, or ``dokumen``.

    Deterministic and pure; never appends a number. ``text`` with no Latin letter
    or digit gives the slug of ``fallback`` by the same rules.
    """
    for candidate in (text, fallback):
        slug = _cut(_slugify(candidate), max_length)
        if slug:
            return slug
    return DEFAULT_FALLBACK
