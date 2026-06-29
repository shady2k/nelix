"""Generic frame fingerprints (spec §7.4 — two fingerprints split from one).

Pure hashers over row slices. The DRIVER points at the regions it knows (its input row span,
its prompt region, its heartbeat region); this module never reads driver-specific chrome. All
fingerprints are short stable hex digests so they can ride in the observability trail.
"""
import hashlib


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def semantic_fp(normalized_frame: str) -> str:
    """Hash of the WHOLE meaning-normalized frame (chrome already zeroed by the driver)."""
    return _hash(normalized_frame)


def region_fp(normalized_frame: str, *, exclude=None, keep=None) -> str:
    """Hash a row slice of the normalized frame.

    - `exclude=(start, end)` drops that half-open row span (derives `content_fp`: exclude the
      active input region, so "did real executor output change" ignores our own echo).
    - `keep=(start, end)` keeps ONLY that row span (derives `prompt_fp`: only the prompt region,
      so "did the published prompt change / leave").
    - neither -> hashes the whole frame (== semantic_fp).
    `exclude` and `keep` are mutually exclusive; `keep` wins if both are given.
    """
    rows = normalized_frame.split("\n")
    if keep is not None:
        s, e = keep
        selected = rows[s:e]
    elif exclude is not None:
        s, e = exclude
        selected = rows[:s] + rows[e:]
    else:
        selected = rows
    return _hash("\n".join(selected))
