from daemon.fingerprints import semantic_fp, region_fp


def test_semantic_fp_stable_and_distinct():
    assert semantic_fp("a\nb") == semantic_fp("a\nb")
    assert semantic_fp("a\nb") != semantic_fp("a\nc")


def test_semantic_fp_is_short_hex():
    fp = semantic_fp("hello world")
    assert len(fp) == 16 and all(c in "0123456789abcdef" for c in fp)


def test_content_fp_ignores_excluded_region():
    # Two frames identical except INSIDE the excluded row span -> equal content_fp.
    base = "line0\nline1\nINPUT-A\nline3"
    other = "line0\nline1\nINPUT-B\nline3"
    exclude = (2, 3)   # exclude row index 2 (the input region)
    assert region_fp(base, exclude=exclude) == region_fp(other, exclude=exclude)
    # A change OUTSIDE the excluded region still differs.
    changed = "line0\nCHANGED\nINPUT-A\nline3"
    assert region_fp(base, exclude=exclude) != region_fp(changed, exclude=exclude)


def test_prompt_fp_changes_when_prompt_region_changes():
    # prompt_fp keeps only the prompt region (keep=span).
    a = "out\nout\n❯ 1. Yes\n  2. No"
    b = "out\nout\n❯ 1. Enrich all three\n  2. Verify-only"
    keep = (2, 4)
    assert region_fp(a, keep=keep) != region_fp(b, keep=keep)
    # Identical prompt region but different scrollback -> same prompt_fp.
    c = "DIFFERENT\nSCROLL\n❯ 1. Yes\n  2. No"
    assert region_fp(a, keep=keep) == region_fp(c, keep=keep)


def test_region_fp_no_span_equals_semantic():
    assert region_fp("a\nb\nc") == semantic_fp("a\nb\nc")
