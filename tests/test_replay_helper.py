import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._replay import replay_frames, replay_observations
from daemon.observation import ObservationCtx

_RAW = (Path(__file__).parent / "golden" / "claude" / "_regression" / "s-2190cfb2-remint.raw").read_bytes()
_CTX = ObservationCtx(last_submitted_text=None, child_alive=True, exit_code=None)

def test_replay_frames_yields_changing_frames():
    frames = list(replay_frames(_RAW))
    assert len(frames) > 10
    assert all(isinstance(off, int) and isinstance(fr, str) for off, fr in frames)

def test_replay_observations_classifies_a_free_text_prompt_somewhere():
    kinds = {o.prompt_kind for _, o in replay_observations(_RAW, _CTX)}
    assert "free_text" in kinds
