from daemon.observation import Observation, ObservationCtx, Option, Heartbeat


def test_observation_defaults_busy():
    o = Observation(prompt_kind="none")
    assert o.prompt_kind == "none"
    assert o.affordances == frozenset()
    assert o.options == ()
    assert o.submitted_echo_present is False
    assert o.busy_reason is None
    assert o.heartbeat.present is False and o.heartbeat.fp is None


def test_observation_modal_carries_options():
    o = Observation(prompt_kind="modal_choice",
                    affordances=frozenset({"modal_choice"}),
                    options=(Option("1", "Enrich all three"), Option("2", "Verify-only")))
    assert o.options[0].id == "1" and o.options[0].label == "Enrich all three"


def test_ctx_shape():
    c = ObservationCtx(last_submitted_text="hi", child_alive=True, exit_code=None)
    assert c.last_submitted_text == "hi" and c.child_alive is True


def test_observation_fingerprint_fields_present():
    o = Observation(prompt_kind="free_text", semantic_fp="aa", content_fp="bb", prompt_fp="cc")
    assert (o.semantic_fp, o.content_fp, o.prompt_fp) == ("aa", "bb", "cc")


def test_heartbeat_defaults_and_population():
    h = Heartbeat()
    assert h.fp is None and h.present is False and h.expected_to_change is False
    h2 = Heartbeat("h1", True, True)
    assert h2.fp == "h1" and h2.present is True and h2.expected_to_change is True


def test_observation_is_frozen():
    import dataclasses
    o = Observation(prompt_kind="none")
    try:
        o.prompt_kind = "free_text"
        assert False, "Observation must be frozen/immutable"
    except dataclasses.FrozenInstanceError:
        pass
