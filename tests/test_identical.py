"""The draw-for-draw check's loader: the team model at a commit imports as a module (the
sampling itself needs PyMC and runs in the CI model job)."""

from pathlib import Path

from adatta.model import identical


def test_the_model_at_a_commit_loads(tmp_path: Path) -> None:
    mod = identical.at_ref("HEAD", tmp_path)
    assert callable(mod.prepare) and callable(mod.sample)
    fd = mod.prepare(identical.synthetic(), identical.LEAGUES,
                     identical.synthetic()["kickoff"][900], 1095, 3.0)  # fmt: skip
    assert len(fd.hg) > 0
