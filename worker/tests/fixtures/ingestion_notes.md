---
title: Ingestion notes
---

# Ingestion notes

A short document used to exercise Markdown structure handling.

## Chunking

Chunks are built from spans of the extracted text, never from rebuilt strings. This is what
makes the offset invariant hold across overlap.

- Headings start a new chunk.
- Sentences are the smallest unit the packer moves.
- Pre-formatted blocks are never split internally.

## Embeddings

Every vector is checked for dimension and for unit norm before it is stored.

```python
def check(vector):
    assert len(vector) == 1024
```

Setext heading
--------------

The final section exists only to prove that setext headings are recognised too.
