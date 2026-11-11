"""Dataset download, caching, parsing and deterministic sampling.

Sources (fetched at run time, never committed):

* GPQA Diamond: ``dataset.zip`` in https://github.com/idavidrein/gpqa (password
  protected; the password is read from that repository's README at run time).
* SimpleQA: https://openaipublic.blob.core.windows.net/simple-evals/simple_qa_test_set.csv

The GPQA zip stays encrypted in the cache; ``gpqa_diamond.csv`` is only read into
memory.
"""

from __future__ import annotations

import csv
import hashlib
import io
import random
import re
import time
import urllib.request
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import platformdirs

Dataset = Literal["gpqa", "simpleqa"]
DATASETS: tuple[Dataset, ...] = ("gpqa", "simpleqa")

GPQA_ZIP_URL = "https://github.com/idavidrein/gpqa/raw/main/dataset.zip"
GPQA_README_URL = "https://raw.githubusercontent.com/idavidrein/gpqa/main/README.md"
GPQA_DIAMOND_MEMBER = "dataset/gpqa_diamond.csv"
SIMPLEQA_URL = "https://openaipublic.blob.core.windows.net/simple-evals/simple_qa_test_set.csv"

LETTERS = ("A", "B", "C", "D")

# From openai/simple-evals common.py (MIT License, Copyright (c) 2024 OpenAI),
# commit 652c89d0ca9df547706735883097e9537d40dc47, QUERY_TEMPLATE_MULTICHOICE.
GPQA_TEMPLATE = (
    "Answer the following multiple choice question. The last line of your response "
    "should be of the following format: 'Answer: $LETTER' (without quotes) where LETTER "
    "is one of ABCD. Think step by step before answering.\n\n"
    "{question}\n\n"
    "A) {A}\nB) {B}\nC) {C}\nD) {D}"
)

_DOWNLOAD_TIMEOUT_S = 120
_DOWNLOAD_ATTEMPTS = 3
_PASSWORD_RE = re.compile(r"password[^`\n]*`([^`\n]+)`", re.IGNORECASE)


class DataError(Exception):
    """A dataset could not be downloaded or parsed."""


@dataclass(frozen=True)
class EvalItem:
    """One question, rendered for the members.

    Attributes:
        dataset: ``gpqa`` or ``simpleqa``.
        item_id: GPQA ``Record ID`` or ``simpleqa-<row index>`` (0-based, official CSV).
        qhash: First 16 hex chars of the SHA-256 of the raw question (safe to publish).
        question: Raw question text (kept in memory; never persisted for GPQA).
        task: The prompt sent to the members (GPQA: multiple choice template).
        gold: GPQA: the correct letter after shuffling. SimpleQA: the gold answer.
    """

    dataset: Dataset
    item_id: str
    qhash: str
    question: str
    task: str
    gold: str


def cache_dir() -> Path:
    """The dataset cache directory (platformdirs user cache, outside the repository)."""
    path = Path(platformdirs.user_cache_dir("llm-crossjury", appauthor=False)) / "evals"
    path.mkdir(parents=True, exist_ok=True)
    return path


def question_hash(question: str) -> str:
    """Short, publishable fingerprint of a question text."""
    return hashlib.sha256(question.strip().encode("utf-8")).hexdigest()[:16]


def _download(url: str, target: Path) -> Path:
    if target.is_file() and target.stat().st_size > 0:
        return target
    if not url.startswith("https://"):
        raise DataError(f"refusing non-https URL {url}")
    tmp = target.with_suffix(target.suffix + ".part")
    last: OSError | None = None
    for attempt in range(_DOWNLOAD_ATTEMPTS):
        try:
            with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_S) as response:  # noqa: S310 - https only, checked above
                tmp.write_bytes(response.read())
        except OSError as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
            continue
        tmp.replace(target)
        return target
    raise DataError(f"download failed: {url} ({last})") from last


def gpqa_password(readme_text: str) -> str:
    """Extract the zip password from the GPQA README (the first back-quoted value on a
    line that mentions the password).

    Raises:
        DataError: no password found.
    """
    match = _PASSWORD_RE.search(readme_text)
    if match is None:
        raise DataError("could not find the dataset.zip password in the GPQA README")
    return match.group(1).strip()


def gpqa_rows(directory: Path | None = None) -> list[dict[str, str]]:
    """Download (once) and read the GPQA Diamond rows."""
    base = directory or cache_dir()
    readme = _download(GPQA_README_URL, base / "gpqa_README.md")
    archive = _download(GPQA_ZIP_URL, base / "gpqa_dataset.zip")
    password = gpqa_password(readme.read_text(encoding="utf-8"))
    try:
        with zipfile.ZipFile(archive) as zf:
            raw = zf.read(GPQA_DIAMOND_MEMBER, pwd=password.encode("utf-8"))
    except (KeyError, RuntimeError, zipfile.BadZipFile) as exc:
        raise DataError(f"cannot read {GPQA_DIAMOND_MEMBER} ({type(exc).__name__})") from exc
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))


def simpleqa_rows(directory: Path | None = None) -> list[dict[str, str]]:
    """Download (once) and read the SimpleQA rows."""
    base = directory or cache_dir()
    path = _download(SIMPLEQA_URL, base / "simple_qa_test_set.csv")
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def shuffle_options(
    correct: str, incorrect: Sequence[str], *, seed: int, item_id: str
) -> tuple[dict[str, str], str]:
    """Shuffle the four GPQA options deterministically per ``(seed, item_id)``.

    Returns:
        ``(letter -> option text, correct letter)``.
    """
    options = [correct.strip(), *(text.strip() for text in incorrect)]
    if len(options) != len(LETTERS):
        raise DataError(f"GPQA item {item_id} does not have four options")
    order = list(range(len(options)))
    random.Random(f"{seed}:{item_id}").shuffle(order)  # noqa: S311 - not security sensitive
    mapping = {letter: options[index] for letter, index in zip(LETTERS, order, strict=True)}
    gold = LETTERS[order.index(0)]
    return mapping, gold


def gpqa_items(rows: Sequence[dict[str, str]], *, seed: int) -> list[EvalItem]:
    """Turn GPQA rows into multiple-choice items with options shuffled under ``seed``."""
    items: list[EvalItem] = []
    for row in rows:
        item_id = row["Record ID"].strip()
        question = row["Question"].strip()
        mapping, gold = shuffle_options(
            row["Correct Answer"],
            [row["Incorrect Answer 1"], row["Incorrect Answer 2"], row["Incorrect Answer 3"]],
            seed=seed,
            item_id=item_id,
        )
        task = GPQA_TEMPLATE.format(question=question, **mapping)
        items.append(
            EvalItem(
                dataset="gpqa",
                item_id=item_id,
                qhash=question_hash(question),
                question=question,
                task=task,
                gold=gold,
            )
        )
    return items


def simpleqa_items(rows: Sequence[dict[str, str]]) -> list[EvalItem]:
    """Turn SimpleQA rows into items; the task is the bare question (as simple-evals)."""
    items: list[EvalItem] = []
    for index, row in enumerate(rows):
        question = row["problem"].strip()
        items.append(
            EvalItem(
                dataset="simpleqa",
                item_id=f"simpleqa-{index}",
                qhash=question_hash(question),
                question=question,
                task=question,
                gold=row["answer"].strip(),
            )
        )
    return items


def sample_items(items: Sequence[EvalItem], n: int, seed: int) -> list[EvalItem]:
    """Deterministic sample: sort by id, shuffle with ``seed``, take the first ``n``.

    A smaller ``n`` with the same seed is always a prefix of a larger one, so a pilot's
    questions are part of the full run.
    """
    ordered = sorted(items, key=lambda item: item.item_id)
    random.Random(seed).shuffle(ordered)  # noqa: S311 - not security sensitive
    return ordered[: max(0, n)]


def load_items(dataset: Dataset, *, n: int, seed: int) -> list[EvalItem]:
    """Download if needed, parse and sample ``n`` items of ``dataset``."""
    if dataset == "gpqa":
        return sample_items(gpqa_items(gpqa_rows(), seed=seed), n, seed)
    return sample_items(simpleqa_items(simpleqa_rows()), n, seed)
