"""Pure scoring helpers: letter parsing, majority vote and flag precision."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

_ANSWER_RE = re.compile(
    r"answer[*_ \t]*[:\uff1a][*_ \t]*\$?\(?([A-D])\)?\$?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_BARE_RE = re.compile(r"^[*_ \t(]*([A-D])[*_ \t).]*$")


def parse_letter(text: str | None) -> str | None:
    """Extract the chosen GPQA letter from a response.

    Takes the LAST ``Answer: X`` (case-insensitive; Markdown emphasis, ``$`` and
    parentheses around the letter allowed), since the requested format puts it on the
    last line and a synthesis may quote earlier answers. Falls back to a response that
    is only a letter. Returns ``None`` when no letter can be found.
    """
    if not text:
        return None
    matches = _ANSWER_RE.findall(text)
    if matches:
        return str(matches[-1]).upper()
    bare = _BARE_RE.match(text.strip())
    return bare.group(1).upper() if bare else None


def plurality_vote(choices: Sequence[str | None]) -> str | None:
    """Most common non-``None`` choice; ``None`` when there is none or the top is tied.

    Rule for the report: a tie for first place (e.g. 2-2) counts as no answer, i.e.
    wrong.
    """
    counts = Counter(c for c in choices if c is not None)
    if not counts:
        return None
    ranked = counts.most_common()
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def strict_majority_correct(correct: Sequence[bool]) -> bool:
    """SimpleQA majority vote: right only if MORE than half of all members are graded
    correct (failed members count as not correct).

    Free-text answers cannot be grouped without another model call; since correct
    answers agree in meaning, a strict majority of correct grades means a majority
    vote over answer meanings picks the gold answer. With no strict majority the vote
    counts as wrong (a lower bound for a plurality vote).
    """
    return sum(1 for c in correct if c) * 2 > len(correct)


@dataclass(frozen=True)
class FlagStats:
    """Question-level flag quality.

    ``error`` means the synthesis final answer was not correct.
    """

    total: int
    flagged: int
    flagged_errors: int
    unflagged: int
    unflagged_errors: int

    @property
    def coverage(self) -> float | None:
        """Share of questions flagged."""
        return self.flagged / self.total if self.total else None

    @property
    def flagged_error_rate(self) -> float | None:
        """Error rate among flagged answers (flag precision)."""
        return self.flagged_errors / self.flagged if self.flagged else None

    @property
    def unflagged_error_rate(self) -> float | None:
        """Error rate among unflagged answers."""
        return self.unflagged_errors / self.unflagged if self.unflagged else None

    @property
    def error_recall(self) -> float | None:
        """Share of all errors that were flagged."""
        errors = self.flagged_errors + self.unflagged_errors
        return self.flagged_errors / errors if errors else None

    @property
    def lift(self) -> float | None:
        """Flagged error rate divided by unflagged error rate."""
        f, u = self.flagged_error_rate, self.unflagged_error_rate
        if f is None or u is None or u == 0:
            return None
        return f / u


def flag_stats(rows: Sequence[tuple[bool, bool]]) -> FlagStats:
    """Compute :class:`FlagStats` from ``(flagged, correct)`` pairs."""
    flagged = [correct for is_flagged, correct in rows if is_flagged]
    unflagged = [correct for is_flagged, correct in rows if not is_flagged]
    return FlagStats(
        total=len(rows),
        flagged=len(flagged),
        flagged_errors=sum(1 for c in flagged if not c),
        unflagged=len(unflagged),
        unflagged_errors=sum(1 for c in unflagged if not c),
    )


def accuracy(values: Sequence[bool]) -> float | None:
    """Share of ``True``; ``None`` for an empty sequence."""
    return sum(1 for v in values if v) / len(values) if values else None
