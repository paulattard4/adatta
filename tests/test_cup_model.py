"""Stage A, A3: cup rows added to P's model (model.cup_strength). Needs PyMC (model group,
Linux / Docker, D13); skipped elsewhere."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

pytest.importorskip("pymc")

from pymc.pytensorf import inputvars  # noqa: E402
from test_backtest import LEAGUES, matches  # noqa: E402
from test_cup_train import ties  # noqa: E402

from adatta.model import cup_strength as cs  # noqa: E402
from adatta.model import team_strength as ts  # noqa: E402

AS_OF = datetime(2019, 12, 2, tzinfo=UTC)


def _fd_rows() -> tuple[ts.FitData, cs.CupRows]:
    fd = ts.prepare(matches(), LEAGUES, AS_OF, 1095, 3.0)
    cr, _ = cs.cup_rows(fd, ties(), AS_OF, 1095, 3.0, 2024)
    return fd, cr


def test_the_rebuilt_strength_path_is_ps() -> None:
    fd, cr = _fd_rows()
    model = ts.build(fd, structure="strength_open")
    attack = cs.add_cups(model, fd, cr)
    rng = np.random.default_rng(3)
    point = {k: rng.normal(0, 0.5, np.shape(v)) for k, v in model.initial_point().items()}
    outs = [attack[-1], model["attack_last"]]
    # the compiled function takes only the value variables these outputs depend on
    need = {v.name for v in inputvars(model.replace_rvs_by_values(outs))}
    for rv in ("strength_z0", "strength_z", "open_z0", "sigma_walk", "sigma_open"):
        assert any(n == rv or (n.startswith(f"{rv}_") and n.endswith("__")) for n in need), rv
    f = model.compile_fn(outs, point_fn=True)
    rebuilt, ps = f({k: v for k, v in point.items() if k in need})
    assert np.allclose(rebuilt, ps)


def test_cups_add_delta_and_observations_and_nothing_else() -> None:
    fd, cr = _fd_rows()
    assert cr.n > 0 and cr.neutral.any()
    plain = ts.build(fd, structure="strength_open", xg=True)
    a3 = cs.build(fd, cr, xg=True)
    plain_rvs = {v.name for v in plain.free_RVs}
    assert {v.name for v in a3.free_RVs} == plain_rvs | {"delta_cup"}
    assert {v.name for v in a3.observed_RVs} - {v.name for v in plain.observed_RVs} == {
        "cup_home_goals",
        "cup_away_goals",
    }
    none = ts.build(fd, structure="strength_open")
    empty = cs.CupRows(*(getattr(cr, f)[:0] for f in cs.CupRows.__dataclass_fields__))
    cs.add_cups(none, fd, empty)
    assert {v.name for v in none.free_RVs} == {
        v.name for v in ts.build(fd, structure="strength_open").free_RVs
    }


def test_only_p_takes_cup_rows() -> None:
    fd, cr = _fd_rows()
    with pytest.raises(ValueError, match="P"):
        cs.add_cups(ts.build(fd, structure="strength_open", shifts=True), fd, cr)
    with pytest.raises(ValueError, match="P"):
        cs.add_cups(ts.build(fd, structure="independent"), fd, cr)
