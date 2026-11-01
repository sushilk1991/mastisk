"""Shared score bounds for synthesis publication; model confidence is not evidence."""
from __future__ import annotations

import math

_CRITIC_MIN_SCORE = 1.0
_CRITIC_MAX_SCORE = 5.0
_PUBLISH_MIN_SCORE = 4.0
_MAX_REVISIONS = 1


def _score_passes(score: float | None) -> bool:
    """Fail closed on missing/invalid grades; e.g. 3.9 withholds and 4.0 passes."""
    return (
        score is not None
        and math.isfinite(score)
        and _PUBLISH_MIN_SCORE <= score <= _CRITIC_MAX_SCORE
    )
