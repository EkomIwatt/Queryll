"""A retrieval-quality harness for the chunker.

Unit tests prove the chunker does not crash and that its offsets are exact. They cannot tell
you whether a chunking change made the product *better*, and chunking is a quality problem —
a boundary moved fifty tokens earlier can be the difference between a citation that answers
the question and one that stops just before the answer.

So: a fixture document, a handful of questions, and the passage that should win. The harness
chunks the document, ranks the chunks against each question, and reports where the
answer-bearing chunk landed. Run it before and after a chunking change and the number tells
you which way you moved.

**The default scorer is lexical, not semantic**, and that is deliberate. Contract 4 forbids
any test from calling the live Voyage API, and the deterministic fake embedder produces noise
— ranking against it would measure nothing. A BM25-style scorer runs offline, is fully
deterministic, and still answers the question the harness is really asking: *is the answer
contained in one coherent, self-sufficient chunk?* A passage split across two chunks, or
buried in a chunk full of unrelated material, ranks badly under lexical scoring for exactly
the same reason it ranks badly under a real embedding.

`tools/quality_harness.py` runs the same cases against the real Voyage client when a key is
present — that is the version to run at merge time, alongside the ★ cosine probe.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from queryll_worker.chunking import ChunkCandidate

_WORD_RE = re.compile(r"[a-z0-9]+")

Scorer = Callable[[str, Sequence[ChunkCandidate]], list[float]]


@dataclass(frozen=True)
class QualityCase:
    """One question and the text that has to be in the chunk that answers it."""

    question: str
    #: A distinctive phrase from the passage that should win. Matched case-insensitively on
    #: whitespace-collapsed text, so it survives line rejoining and de-hyphenation.
    expected_phrase: str


@dataclass(frozen=True)
class CaseResult:
    question: str
    #: 1-based rank of the best chunk containing the expected phrase, or None if no chunk does.
    rank: int | None
    #: How many chunks contain the phrase. More than one means overlap duplicated it, which is
    #: expected; zero means chunking split the passage and the answer is no longer retrievable.
    containing: int
    winner_ordinal: int | None


@dataclass(frozen=True)
class QualityReport:
    results: tuple[CaseResult, ...]

    @property
    def hit_at_1(self) -> float:
        return self._share(lambda r: r.rank == 1)

    @property
    def hit_at_3(self) -> float:
        return self._share(lambda r: r.rank is not None and r.rank <= 3)

    @property
    def coverage(self) -> float:
        """Share of cases where *any* chunk contains the whole expected passage."""
        return self._share(lambda r: r.containing > 0)

    @property
    def mrr(self) -> float:
        if not self.results:
            return 0.0
        return sum(0.0 if r.rank is None else 1.0 / r.rank for r in self.results) / len(
            self.results
        )

    def _share(self, predicate: Callable[[CaseResult], bool]) -> float:
        if not self.results:
            return 0.0
        return sum(1 for result in self.results if predicate(result)) / len(self.results)

    def format(self) -> str:
        lines = [
            f"coverage={self.coverage:.2f} hit@1={self.hit_at_1:.2f} "
            f"hit@3={self.hit_at_3:.2f} mrr={self.mrr:.3f}",
        ]
        for result in self.results:
            rank = "miss" if result.rank is None else f"#{result.rank}"
            lines.append(
                f"  {rank:>5}  in={result.containing}  {result.question}"
            )
        return "\n".join(lines)


def _tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def lexical_scorer(question: str, chunks: Sequence[ChunkCandidate]) -> list[float]:
    """BM25-style relevance, computed over the chunk set itself.

    Deterministic and offline. Not a stand-in for a real embedding — a stand-in for *asking
    whether the answer is in one piece*.
    """
    if not chunks:
        return []

    documents = [_tokenize(chunk.text) for chunk in chunks]
    lengths = [len(tokens) for tokens in documents]
    average_length = sum(lengths) / len(lengths) if lengths else 0.0
    frequencies = [Counter(tokens) for tokens in documents]

    document_frequency: Counter[str] = Counter()
    for tokens in documents:
        document_frequency.update(set(tokens))

    k1, b = 1.5, 0.75
    total = len(documents)
    scores: list[float] = []
    for index, counts in enumerate(frequencies):
        score = 0.0
        for term in set(_tokenize(question)):
            appearances = document_frequency.get(term, 0)
            if appearances == 0:
                continue
            idf = math.log(1.0 + (total - appearances + 0.5) / (appearances + 0.5))
            frequency = counts.get(term, 0)
            if frequency == 0:
                continue
            denominator = frequency + k1 * (
                1 - b + b * (lengths[index] / average_length if average_length else 1.0)
            )
            score += idf * (frequency * (k1 + 1)) / denominator
        scores.append(score)
    return scores


def evaluate(
    chunks: Sequence[ChunkCandidate],
    cases: Sequence[QualityCase],
    scorer: Scorer = lexical_scorer,
) -> QualityReport:
    """Rank `chunks` against each case and report where the answer-bearing chunk landed."""
    results: list[CaseResult] = []
    for case in cases:
        needle = _normalize(case.expected_phrase)
        containing = [
            index
            for index, chunk in enumerate(chunks)
            if needle in _normalize(chunk.text)
        ]
        scores = scorer(case.question, chunks)
        # Ties break on ordinal so the report is stable across runs.
        order = sorted(range(len(chunks)), key=lambda i: (-scores[i], chunks[i].ordinal))

        rank: int | None = None
        for position, index in enumerate(order, start=1):
            if index in containing:
                rank = position
                break

        results.append(
            CaseResult(
                question=case.question,
                rank=rank,
                containing=len(containing),
                winner_ordinal=chunks[order[0]].ordinal if order else None,
            )
        )
    return QualityReport(results=tuple(results))
