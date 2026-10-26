"""Feature switches: one list in `FEATURES_OFF`, read the same way by every service (mib-infra#107).

A feature outside the agreed scope is switched off from configuration, with its code
left in place and no release needed. The value is a comma-separated list of names
(empty means everything on); `mib-infra` defines it once and passes it to each
service that serves or guards a feature, so the UI and the API cannot disagree.

**Names are dotted by surface:** `admin.<feature>` and `user.<feature>`. `FEATURES`
is the fixed list. A name outside it is an error at start (`parse_features_off`), so
a typo cannot leave a feature on by accident. The same list is documented in
`mib-infra/docs/feature-flags.md`; change both together.

**This is a global kill switch, not an entitlement.** Whether a plan may use a
capability (`regulations.compile_compare`, `ai.summary`) is `require_capability`'s
question and is unchanged.

**What a switched-off route answers:** a 404 in the standard envelope, the same as
a route that does not exist. For an `admin.*` feature only the write routes carry
the dependency: the reads other parts of the system consume stay on, so "off"
means "cannot be edited from the admin", never "not running". A `user.*` feature
is refused for every method.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable

from fastapi import HTTPException, status

from mib_shared.telemetry import get_logger

logger = get_logger(__name__)

ADMIN_FEATURES = frozenset(
    {
        "admin.content",
        "admin.plans",
        "admin.limits",
        "admin.tax",
        "admin.maintenance",
        "admin.ingestion",
        "admin.administrators",
        "admin.lms",
    }
)
USER_FEATURES = frozenset(
    {
        "user.ai_chat",
        "user.ai_summary",
        "user.semantic_search",
        "user.decisions",
        "user.decisions_pk",
        "user.treaties",
        "user.rates",
        "user.interest_calculator",
        "user.tax_calendar",
        "user.lms",
        "user.pricing",
        "user.signup",
        "user.reading_pdf",
    }
)
FEATURES = ADMIN_FEATURES | USER_FEATURES


class UnknownFeatureError(ValueError):
    """A name outside `FEATURES`. A `ValueError`, so a settings validator reports it at start."""


def parse_features_off(raw: str | Iterable[str] | None) -> frozenset[str]:
    """The set of switched-off features in a `FEATURES_OFF` value.

    Names are trimmed, blanks and repeats are ignored, and an empty or missing value
    is an empty set. Raises `UnknownFeatureError` naming every unknown entry, so a
    service whose settings parse this fails at start with a message that says what
    to fix.
    """
    if raw is None:
        return frozenset()
    names = raw.split(",") if isinstance(raw, str) else raw
    wanted = {name.strip() for name in names if name.strip()}
    unknown = sorted(wanted - FEATURES)
    if unknown:
        raise UnknownFeatureError(
            f"FEATURES_OFF names unknown feature(s): {', '.join(unknown)}. "
            f"Valid names: {', '.join(sorted(FEATURES))}."
        )
    return frozenset(wanted)


def feature_enabled(name: str, features_off: frozenset[str]) -> bool:
    """Whether `name` is on. A name outside `FEATURES` is a programming error, not an answer."""
    _known(name)
    return name not in features_off


def require_feature(name: str, features_off: Callable[[], frozenset[str]]):
    """FastAPI dependency: 404 while `name` is switched off, a no-op otherwise.

    `features_off` is called per request rather than captured, so a service passes
    its settings accessor and a test can change the setting. Add it to a route with
    `dependencies=[Depends(require_feature("admin.tax", ...))]`, or to a router.
    A misspelt `name` raises here, when the route is declared, never at the first request.
    """
    _known(name)

    async def _dependency() -> None:
        """404, the same as a missing route; only the log line says which switch is off."""
        if name in features_off():
            logger.info("feature_disabled", feature=name)
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")

    return _dependency


def _known(name: str) -> None:
    if name not in FEATURES:
        raise UnknownFeatureError(f"{name!r} is not in mib_shared.features.FEATURES.")


__all__ = [
    "ADMIN_FEATURES",
    "FEATURES",
    "USER_FEATURES",
    "UnknownFeatureError",
    "feature_enabled",
    "parse_features_off",
    "require_feature",
]
