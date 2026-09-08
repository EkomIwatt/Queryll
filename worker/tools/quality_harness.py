"""Run the retrieval-quality harness from the command line.

Two modes, and the difference between them is the point:

    python tools/quality_harness.py                    # lexical scorer, offline, in CI
    python tools/quality_harness.py --voyage           # the real embedding client

The lexical run is what `tests/test_quality.py` enforces as a floor. It answers "did chunking
keep the answer in one piece?" and it runs anywhere, deterministically, without a key.

The `--voyage` run is a **merge-time** tool, not a test — Contract 4 forbids any test from
calling the live API. It is the second half of the ★ cross-process probe: if the cosine probe
passes but this reports junk rankings, the divergence is in `input_type`, not in the model.

    VOYAGE_API_KEY=... python tools/quality_harness.py --voyage \
        --document tests/fixtures/paper_two_column.pdf
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from queryll_worker.chunking import ChunkCandidate, chunk_document  # noqa: E402
from queryll_worker.config import (  # noqa: E402
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    ChunkingSettings,
)
from queryll_worker.embeddings.base import l2_norm  # noqa: E402
from queryll_worker.embeddings.voyage import VoyageEmbedder  # noqa: E402
from queryll_worker.parsing import extract  # noqa: E402
from queryll_worker.quality import QualityCase, evaluate, lexical_scorer  # noqa: E402

DEFAULT_DOCUMENT = ROOT / "tests" / "fixtures" / "paper_two_column.pdf"

#: The same cases the test suite enforces, so the two modes are comparable.
CASES = (
    QualityCase(
        "How many support articles were in the sampling frame?",
        "The sampling frame was drawn from 4,318 publicly available support articles",
    ),
    QualityCase(
        "What chunk size and overlap performed best?",
        "Chunks of roughly 512 tokens with 64 tokens of overlap outperformed both",
    ),
    QualityCase(
        "What effect did stripping running headers have on mean reciprocal rank?",
        "Stripping running headers and footers before embedding improved mean reciprocal",
    ),
    QualityCase(
        "How were annotator disagreements resolved?",
        "disagreements were resolved by discussion",
    ),
)


def _mime_for(path: Path) -> str:
    return {
        ".pdf": "application/pdf",
        ".md": "text/markdown",
        ".markdown": "text/markdown",
    }.get(path.suffix.lower(), "text/plain")


async def _voyage_scores(
    questions: Sequence[str], chunks: Sequence[ChunkCandidate]
) -> dict[str, list[float]]:
    """Embed the chunks as documents and each question as a query, then score by cosine.

    Note the asymmetry: `input_type="document"` for the passages, `"query"` for the questions.
    The worker itself never embeds a query — that half lives in the API process — so this is
    the only place in this repository where both sides of Contract 4 are exercised at once,
    and it is a tool rather than a test for exactly that reason.
    """
    import httpx

    from queryll_worker.config import VOYAGE_API_URL
    from queryll_worker.embeddings.base import check_vector

    api_key = os.environ["VOYAGE_API_KEY"]
    model = os.environ.get("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
    dimension = int(os.environ.get("EMBEDDING_DIM", DEFAULT_EMBEDDING_DIM))

    embedder = VoyageEmbedder(api_key, model=model, dimension=dimension)
    document_vectors: list[list[float]] = []
    try:
        for start in range(0, len(chunks), 128):
            batch = chunks[start : start + 128]
            document_vectors.extend(
                await embedder.embed_documents([chunk.text for chunk in batch])
            )
    finally:
        await embedder.aclose()

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            VOYAGE_API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "input": list(questions),
                "model": model,
                "input_type": "query",
                "output_dtype": "float",
            },
        )
        response.raise_for_status()
        payload = response.json()

    ordered = sorted(payload["data"], key=lambda item: item["index"])
    query_vectors = [item["embedding"] for item in ordered]
    for vector in query_vectors:
        check_vector(vector, dimension)

    scores: dict[str, list[float]] = {}
    for question, query_vector in zip(questions, query_vectors, strict=True):
        scores[question] = [
            sum(a * b for a, b in zip(query_vector, doc, strict=True))
            for doc in document_vectors
        ]
    print(
        f"embedded {len(document_vectors)} chunks and {len(query_vectors)} queries "
        f"with {model} ({dimension}d, |q|={l2_norm(query_vectors[0]):.4f})"
    )
    return scores


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT)
    parser.add_argument(
        "--voyage",
        action="store_true",
        help="score with the real Voyage API instead of the offline lexical scorer",
    )
    parser.add_argument("--target-tokens", type=int, default=512)
    parser.add_argument("--overlap-tokens", type=int, default=64)
    arguments = parser.parse_args(argv)

    content = arguments.document.read_bytes()
    document = extract(content, _mime_for(arguments.document), max_pages=500)
    chunks = chunk_document(
        document,
        ChunkingSettings(
            target_tokens=arguments.target_tokens,
            overlap_tokens=arguments.overlap_tokens,
        ),
    )
    print(
        f"{arguments.document.name}: {len(document.text)} chars, "
        f"{document.page_count or 0} pages, {len(chunks)} chunks, "
        f"target={arguments.target_tokens} overlap={arguments.overlap_tokens}"
    )

    if arguments.voyage:
        scores = asyncio.run(_voyage_scores([case.question for case in CASES], chunks))
        report = evaluate(chunks, CASES, lambda question, _chunks: scores[question])
    else:
        report = evaluate(chunks, CASES, lexical_scorer)

    print(report.format())
    return 0 if report.coverage == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
