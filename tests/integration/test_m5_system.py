import json

import pytest

from scripts.m5_smoke import Run, scenario


@pytest.mark.parametrize("mode", ["active", "passive"])
@pytest.mark.parametrize("first", ["S1", "S2", "S3"])
def test_three_crashes_are_recovered_by_rm_and_lfd_with_continuous_clients(tmp_path, mode, first):
    run = Run(mode, tmp_path / mode, first)
    try:
        result = scenario(run)
        assert len(result["rounds"]) == 3
        assert result["rounds"][0]["replica"] == result["rounds"][1]["replica"]
        assert result["rounds"][2]["replica"] != first
        (run.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    finally:
        run.close()
