"""Regression test for the installed operations-console command target."""

import importlib
import tomllib
from pathlib import Path


def test_console_entry_point_resolves_to_callable_submodule() -> None:
    with (Path(__file__).parent.parent / "pyproject.toml").open("rb") as file:
        target = tomllib.load(file)["project"]["scripts"]["operations-console"]
    assert target == "dashboard.dashboard:main"
    module_name, attribute = target.split(":", 1)
    assert callable(getattr(importlib.import_module(module_name), attribute))
