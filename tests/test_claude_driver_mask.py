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
