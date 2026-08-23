"""Word Error Rate — a standard ASR accuracy metric.

WER = (substitutions + insertions + deletions) / len(reference words)

Implemented by hand (classic edit-distance DP) rather than pulling in a
library like `jiwer` — this is a small, one-purpose calculation and the
project's own submission guidelines ask for minimal dependencies. This
script isn't part of the shipped app anyway (nothing under backend/
imports it), so it doesn't affect runtime dependencies either way.
"""

import string


def _normalize(text: str) -> list[str]:
    words = text.lower().split()
    return [w.strip(string.punctuation) for w in words if w.strip(string.punctuation)]


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Fraction of reference words that differ, after lowercasing and
    stripping punctuation (standard WER practice — punctuation and casing
    aren't what "accuracy" is meant to measure here)."""
    ref = _normalize(reference)
    hyp = _normalize(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0

    # Standard Levenshtein edit distance at the word level.
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        curr = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, start=1):
            cost = 0 if r == h else 1
            curr[j] = min(
                prev[j] + 1,  # deletion
                curr[j - 1] + 1,  # insertion
                prev[j - 1] + cost,  # substitution / match
            )
        prev = curr

    return prev[len(hyp)] / len(ref)
