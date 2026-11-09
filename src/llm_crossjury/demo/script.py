"""Canned content for demo mode: one sample question, four answers, reviews, a synthesis.

Every answer is a list of :class:`Block` objects. A block becomes exactly one agreement
segment (a paragraph or one list item, see
:func:`llm_crossjury.core.synthesis.agreement.segment_answer`) and carries the *topic*
it speaks to and whether it supports or contradicts that topic's statement. The demo
responder uses the topics to build a realistic agreement judgment (consensus, disputed
and unique groups) whatever anonymous labels a run assigns.

The content is a real statistics question with answers that are accurate except where
a reviewer is meant to catch an error (the "possibly wrong" list). None of it mentions
a model or vendor name, so anonymization never alters it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from llm_crossjury.core.schemas._base import Severity

Stance = Literal["support", "contradict"]

DEMO_QUESTION = (
    "I'm comparing reaction times between two groups of 12 participants each. The "
    "distributions look somewhat right-skewed. Should I use an independent-samples "
    "t-test or a Mann-Whitney U test, and what should I report?"
)
"""The sample question the capture script types and the demo answers."""

DEMO_PROVIDERS: tuple[str, ...] = ("anthropic", "openai", "gemini", "xai")
"""Providers with a canned answer, in display order."""


@dataclass(frozen=True)
class Topic:
    """A point the answers make, stated neutrally (an agreement group)."""

    key: str
    statement: str
    counter: str | None = None
    """Statement used when only contradicting answers are present in a run."""


@dataclass(frozen=True)
class Block:
    """One segment of a canned answer."""

    text: str
    topic: str
    stance: Stance = "support"


@dataclass(frozen=True)
class Issue:
    """A review issue; ``quote`` must appear verbatim in the target answer."""

    quote: str
    problem: str
    severity: Severity
    suggested_fix: str | None


@dataclass(frozen=True)
class ReviewNotes:
    """What every reviewer says about one provider's answer."""

    issues: tuple[Issue, ...]
    strengths: tuple[str, ...]


TOPICS: tuple[Topic, ...] = (
    Topic(
        "skew",
        "Reaction times are usually right-skewed, so look at each group's distribution "
        "before choosing a test.",
    ),
    Topic("effect", "Report an effect size with a confidence interval, not only the p-value."),
    Topic(
        "equal_n",
        "With equal groups of 12, Welch's t-test is fairly robust to mild skew and "
        "unequal variances.",
    ),
    Topic("median_iqr", "Describe each group with its median and interquartile range."),
    Topic(
        "pretest",
        "Test each group for normality (Shapiro-Wilk) and choose the test from the result.",
        counter="Decide the analysis in advance instead of pre-testing for normality.",
    ),
    Topic(
        "primary_mw",
        "The Mann-Whitney U test should be the primary analysis.",
        counter="Welch's t-test should be the primary analysis.",
    ),
    Topic(
        "medians",
        "The Mann-Whitney U test compares the two groups' medians.",
        counter="The Mann-Whitney U test compares medians only if both distributions "
        "have the same shape.",
    ),
    Topic("log", "Log-transform the reaction times and run Welch's t-test on the log scale."),
    Topic("perm", "A permutation test on the mean difference is an assumption-light alternative."),
    Topic(
        "boot",
        "A bootstrap confidence interval for the mean difference does not rely on normality.",
    ),
    Topic(
        "trials",
        "With many trials per participant, average per participant or fit a mixed model.",
    ),
)

ANSWERS: dict[str, tuple[Block, ...]] = {
    "anthropic": (
        Block(
            "Short answer: use Welch's t-test, on log-transformed reaction times if the skew "
            "is clear, and report the effect size with a confidence interval. Keep the "
            "Mann-Whitney U test as a sensitivity check.",
            "primary_mw",
            "contradict",
        ),
        Block(
            "- Reaction times are almost always right-skewed, so plot each group (a "
            "histogram or a Q-Q plot) before you decide anything.",
            "skew",
        ),
        Block(
            "- Log-transforming RTs usually makes them close to normal. A Welch t-test on "
            "log RTs then compares geometric means; say so when you report it.",
            "log",
        ),
        Block(
            "- With equal groups of 12, the t-test is fairly robust to mild skew, and "
            "Welch's version does not assume equal variances.",
            "equal_n",
        ),
        Block(
            "- Don't pick the test with a Shapiro-Wilk test first. At n = 12 it has little "
            "power, and choosing the test from the same data distorts your error rates. "
            "Decide the analysis before you look.",
            "pretest",
            "contradict",
        ),
        Block(
            "- The Mann-Whitney U test does not compare medians in general. It tests "
            "whether values in one group tend to be larger than in the other; it is a "
            "test of medians only if both distributions have the same shape.",
            "medians",
            "contradict",
        ),
        Block(
            "Report the mean difference (or the ratio of geometric means) with a 95% CI, "
            "Hedges' g with its CI, and the exact p-value.",
            "effect",
        ),
    ),
    "openai": (
        Block(
            "Because your data are skewed and the samples are small, the Mann-Whitney U "
            "test is the safer choice.",
            "primary_mw",
        ),
        Block(
            "1. Check normality in each group with a Shapiro-Wilk test. If either group "
            "departs from normality (p < .05), use the Mann-Whitney U test.",
            "pretest",
        ),
        Block(
            "2. The Mann-Whitney U test compares the medians of the two groups, so it is "
            "not distorted by a few very slow responses.",
            "medians",
        ),
        Block(
            "3. Reaction-time data usually have a long right tail, which makes the mean "
            "sensitive to outliers.",
            "skew",
        ),
        Block(
            "4. Report U, the standardized z, the exact p-value, and the rank-biserial "
            "correlation as the effect size, with a 95% confidence interval.",
            "effect",
        ),
        Block(
            "Also give the median and interquartile range of each group, so readers can "
            "see how large the difference is.",
            "median_iqr",
        ),
    ),
    "gemini": (
        Block(
            "The right test depends on whether your data are normally distributed, so "
            "test that first.",
            "pretest",
        ),
        Block(
            "- Run a Shapiro-Wilk test on each group. If p > .05 for both groups, the data "
            "are normal and you can use Student's t-test. Otherwise use the Mann-Whitney U "
            "test.",
            "pretest",
        ),
        Block(
            "- Skewed reaction times are common, and a histogram of each group will show "
            "you how strong the skew is.",
            "skew",
        ),
        Block("- Describe each group with its median and interquartile range.", "median_iqr"),
        Block(
            "- An exact permutation test on the difference in means is a good "
            "assumption-light alternative with samples this small.",
            "perm",
        ),
        Block(
            "Whatever test you use, report an effect size such as Cohen's d or r with a "
            "confidence interval, alongside the p-value.",
            "effect",
        ),
    ),
    "xai": (
        Block(
            "I would default to Welch's t-test here rather than the Mann-Whitney U test.",
            "primary_mw",
            "contradict",
        ),
        Block(
            "- With equal groups of 12 and only mild skew, Welch's t-test keeps its error "
            "rate well, and it does not assume equal variances.",
            "equal_n",
        ),
        Block(
            "- Look at the raw distributions first: reaction times have a long right tail, "
            "and one very slow participant can move a mean a lot.",
            "skew",
        ),
        Block(
            "- Skip formal normality tests. They are underpowered at this sample size and "
            "answer a question you don't need answered.",
            "pretest",
            "contradict",
        ),
        Block(
            "- Report the mean difference with a 95% confidence interval, plus Cohen's d.",
            "effect",
        ),
        Block(
            "- A percentile bootstrap of the mean difference (10,000 resamples) gives a "
            "confidence interval that does not rely on normality.",
            "boot",
        ),
        Block(
            "- If each participant gave many trials, don't run the test on raw trials. "
            "Average per participant first, or fit a mixed model with participant as a "
            "random effect.",
            "trials",
        ),
    ),
}


def answer_text(provider: str) -> str:
    """The canned answer of ``provider`` as Markdown (blocks joined by blank lines)."""
    return "\n\n".join(block.text for block in ANSWERS[provider])


REVIEW_NOTES: dict[str, ReviewNotes] = {
    "anthropic": ReviewNotes(
        issues=(
            Issue(
                quote="Log-transforming RTs usually makes them close to normal.",
                problem="'Usually' is too strong: reaction-time distributions often stay "
                "skewed after a log transform.",
                severity=Severity.MINOR,
                suggested_fix="Check the transformed data with a plot as well.",
            ),
        ),
        strengths=(
            "Explains why choosing the test with a normality pre-test is a mistake.",
            "Corrects the common misreading of the Mann-Whitney U test.",
            "Gives a complete list of what to report.",
        ),
    ),
    "openai": ReviewNotes(
        issues=(
            Issue(
                quote="The Mann-Whitney U test compares the medians of the two groups",
                problem="A common misconception. The test asks whether values in one group "
                "tend to be larger than in the other; it is a test of medians only if both "
                "distributions have the same shape.",
                severity=Severity.MAJOR,
                suggested_fix="Describe it as a test of whether one group tends to have "
                "larger values.",
            ),
            Issue(
                quote="Check normality in each group with a Shapiro-Wilk test.",
                problem="At n = 12 the pre-test has little power, and choosing the test "
                "from its result inflates error rates.",
                severity=Severity.MINOR,
                suggested_fix="Choose the analysis in advance.",
            ),
        ),
        strengths=(
            "Reports the rank-biserial correlation, the right effect size for this test.",
            "Asks for medians and IQRs, which suit skewed data.",
        ),
    ),
    "gemini": ReviewNotes(
        issues=(
            Issue(
                quote="If p > .05 for both groups, the data are normal and you can use "
                "Student's t-test.",
                problem="A non-significant Shapiro-Wilk result does not show normality, "
                "least of all at n = 12. Student's t-test also assumes equal variances.",
                severity=Severity.MAJOR,
                suggested_fix="Use Welch's t-test by default and decide on it in advance.",
            ),
        ),
        strengths=(
            "Suggests a permutation test, which is exact and assumption-light at this sample size.",
        ),
    ),
    "xai": ReviewNotes(
        issues=(
            Issue(
                quote="plus Cohen's d",
                problem="With 12 per group, Cohen's d is biased upward.",
                severity=Severity.MINOR,
                suggested_fix="Report Hedges' g, which corrects the small-sample bias.",
            ),
        ),
        strengths=(
            "Raises the trial-level versus participant-level problem, which can invalidate "
            "the test entirely.",
            "Recommends a bootstrap interval that does not rely on normality.",
        ),
    ),
}

QUALITY_ORDER: tuple[str, ...] = ("anthropic", "xai", "gemini", "openai")
"""Reviewers rank the answers in this order (fewest and mildest issues first)."""

RANKING_RATIONALE = (
    "The ranking follows the issues above: answers with only minor issues rank above "
    "answers with a major error, and among those the answer with fewer problems ranks "
    "higher."
)


@dataclass(frozen=True)
class DemoPosition:
    """One side of a canned dispute."""

    provider: str
    stance: str
    argument: str


@dataclass(frozen=True)
class DemoDispute:
    """A canned dispute for the chair's ``disputed`` list."""

    question: str
    positions: tuple[DemoPosition, ...]
    assessment: str


DISPUTES: tuple[DemoDispute, ...] = (
    DemoDispute(
        question="Should you test for normality first and choose the test from the result?",
        positions=(
            DemoPosition(
                "gemini",
                "Yes. Run a Shapiro-Wilk test on each group; use a t-test if both pass and "
                "the Mann-Whitney U test otherwise.",
                "It matches the test to the distribution of the data.",
            ),
            DemoPosition(
                "openai",
                "Yes. Switch to the Mann-Whitney U test if either group departs from normality.",
                "Skewed data and small samples call for a rank-based test.",
            ),
            DemoPosition(
                "anthropic",
                "No. Decide the analysis in advance.",
                "At n = 12 the pre-test has little power, and choosing the test from the "
                "same data distorts error rates.",
            ),
        ),
        assessment="The final answer follows the position against pre-testing. Two "
        "reviewers flagged the conditional procedure, and it does not protect against "
        "the risk it targets.",
    ),
    DemoDispute(
        question="Which test should be the primary analysis?",
        positions=(
            DemoPosition(
                "openai",
                "The Mann-Whitney U test, because the data are skewed and the samples are small.",
                "It does not rely on normality and resists slow outliers.",
            ),
            DemoPosition(
                "anthropic",
                "Welch's t-test, on log-transformed RTs if the skew is clear, with the "
                "Mann-Whitney U test as a sensitivity check.",
                "Log RTs are often near normal, and the result reads as a ratio of "
                "geometric means.",
            ),
            DemoPosition(
                "xai",
                "Welch's t-test on the raw reaction times.",
                "With equal groups of 12 and mild skew it controls error rates well and "
                "keeps the result in milliseconds.",
            ),
        ),
        assessment="The final answer uses Welch's t-test as the primary analysis and the "
        "Mann-Whitney U test as a sensitivity check. Whether to log-transform depends on "
        "how strong the skew is, so the answer explains both options.",
    ),
)

UNIQUE_NOT_INCLUDED: tuple[tuple[str, str], ...] = (
    (
        "gemini",
        "An exact permutation test on the mean difference is an assumption-light "
        "alternative at this sample size.",
    ),
    (
        "xai",
        "A percentile bootstrap (10,000 resamples) gives a confidence interval for the "
        "mean difference without assuming normality.",
    ),
)
"""Single-answer insights the chair left out of the final answer: ``(provider, text)``."""

FINAL_TEXT = """\
**Use Welch's t-test as the main analysis, and report the Mann-Whitney U test as a \
sensitivity check.** With two groups of 12, the choice of test matters less than three \
habits: look at the data, decide the analysis before testing, and report effect sizes \
with confidence intervals.

### 1. Look at the distributions first

Plot each group (a histogram or a Q-Q plot). Reaction times are almost always \
right-skewed, and one very slow participant can move a mean a lot.

- If the skew is mild, run Welch's t-test on the raw reaction times. The result stays in \
milliseconds.
- If the skew is clear, log-transform the reaction times and run Welch's t-test on the \
log scale. Report the result as a ratio of geometric means.

### 2. Don't choose the test with a normality test

A Shapiro-Wilk test at n = 12 has little power, so "p > .05" does not show that the data \
are normal. Choosing the test from the same data also distorts your error rates. Decide \
the analysis in advance; a short analysis plan is enough.

Prefer Welch's t-test to Student's: with equal group sizes it costs almost nothing when \
the variances are equal and protects you when they are not.

### 3. Read the Mann-Whitney U test correctly

It tests whether values in one group tend to be larger than in the other. It is a test \
of medians only if both distributions have the same shape, so describe it that way in \
your write-up.

### 4. Check the unit of analysis

If each participant gave many trials, average per participant before testing, or fit a \
mixed model with participant as a random effect. A t-test on raw trials treats them as \
independent and overstates your evidence.

### 5. What to report

- Each group's mean and SD, plus the median and IQR (useful for skewed data).
- Welch's t, its degrees of freedom, the exact p-value, and the mean difference with its \
95% CI.
- Hedges' g with a 95% CI (less biased than Cohen's d at this sample size).
- For the sensitivity check: U, the exact p-value, and the rank-biserial correlation.
"""

CONSENSUS_TOPICS: tuple[str, ...] = ("skew", "effect", "equal_n", "median_iqr")
"""Topics the chair lists as consensus (when at least two present answers hold them)."""

__all__ = [
    "ANSWERS",
    "CONSENSUS_TOPICS",
    "DEMO_PROVIDERS",
    "DEMO_QUESTION",
    "DISPUTES",
    "FINAL_TEXT",
    "QUALITY_ORDER",
    "RANKING_RATIONALE",
    "REVIEW_NOTES",
    "TOPICS",
    "UNIQUE_NOT_INCLUDED",
    "Block",
    "DemoDispute",
    "DemoPosition",
    "Issue",
    "ReviewNotes",
    "Topic",
    "answer_text",
]
