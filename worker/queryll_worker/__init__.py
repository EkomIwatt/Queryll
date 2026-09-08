"""Queryll ingestion worker.

A standalone process that turns uploaded document bytes into searchable, citable vectors.
It has no HTTP surface and never talks to the API process: the only medium between the two
is rows in Postgres (Contracts 2 and 3 in the root CLAUDE.md).
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
