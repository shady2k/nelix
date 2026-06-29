from daemon.drivers.claude import ClaudeDriver

d = ClaudeDriver()


def test_volatile_drops_chrome():
    assert d.is_transcript_volatile("✽ Cultivating…")
    assert d.is_transcript_volatile("· Recombobulating… (1m 58s · ↓ 4.0k tokens)")
    assert d.is_transcript_volatile("  esc to interrupt")
    assert d.is_transcript_volatile("  ❯ ")
    assert d.is_transcript_volatile("❯ [Pasted text #1]")
    assert d.is_transcript_volatile("  shift+tab to cycle")
    assert d.is_transcript_volatile("────────────────────")


def test_volatile_keeps_content():
    assert not d.is_transcript_volatile("⏺ I'll start by invoking the TDD skill")
    assert not d.is_transcript_volatile("  Read 4 files")
    assert not d.is_transcript_volatile("internal/conn/dial.go")
    assert not d.is_transcript_volatile("       7 - success pair: after Open+acquireOrReopen")


def test_volatile_ellipsis_in_progress():
    # Claude's in-progress tool-status lines end in "…"; finalized content does not.
    assert d.is_transcript_volatile("⏺ Reading 1 file…")
    assert d.is_transcript_volatile("  Running 1 shell command…")
    assert d.is_transcript_volatile("⏺ Committed 8034c1d, running 1 shell command…")
    # Bare turn marker (no content after ⏺)
    assert d.is_transcript_volatile("⏺")
    assert d.is_transcript_volatile("  ⏺  ")
    # Background hint line
    assert d.is_transcript_volatile("(ctrl+b to run in background)")
    assert d.is_transcript_volatile("  (ctrl+b to run in background)  ")


def test_volatile_keeps_settled_tool_lines():
    # Settled (finalized) tool lines have no trailing ellipsis — must NOT be dropped.
    assert not d.is_transcript_volatile("  Ran 1 shell command")
    assert not d.is_transcript_volatile("⏺ Read 1 file")
    assert not d.is_transcript_volatile("⏺ Готово. Закоммилил T3 на ветке phase-no1.3.")
    assert not d.is_transcript_volatile("internal/conn/dial.go")
