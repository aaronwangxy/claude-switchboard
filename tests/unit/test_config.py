"""Configuration loading, and the promises `config.example.yaml` makes about itself."""

from __future__ import annotations

from pathlib import Path

import pytest

from switchboard.config import CONFIG_ENV, Config, load_config

EXAMPLE = Path(__file__).resolve().parents[2] / "config.example.yaml"


def test_the_example_config_does_not_disable_the_model_environment_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    """Copying the example file must not silently turn off what its comment promises.

    `models.*` uses a `default_factory` reading `SB_STRONG_MODEL`/`SB_FAST_MODEL`, and a
    default factory only fires when the key is *absent*. An example file that writes
    `manager: null` therefore pins every role to None and skips the environment -- the
    exact opposite of the behaviour the file documents two lines above.
    """
    monkeypatch.setenv(CONFIG_ENV, str(EXAMPLE))
    monkeypatch.setenv("SB_STRONG_MODEL", "strong-model")
    monkeypatch.setenv("SB_FAST_MODEL", "fast-model")

    config = load_config()

    assert config.models.manager == "strong-model"
    assert config.models.planner == "strong-model"
    assert config.models.reviewer == "strong-model"
    assert config.models.implementer == "fast-model"
    assert config.models.verifier == "fast-model"
    assert config.models.general == "fast-model"


def test_the_example_config_documents_every_section_of_the_real_model(
    monkeypatch: pytest.MonkeyPatch,
):
    """A key the example omits is a setting nobody discovers."""
    monkeypatch.setenv(CONFIG_ENV, str(EXAMPLE))
    documented = set(load_config().model_dump(mode="json"))
    assert documented == set(Config().model_dump(mode="json"))
    assert EXAMPLE.read_text().count("\n") > 0


def test_the_previous_name_for_the_default_composite_workflow_still_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "config.yaml"
    path.write_text("default_profile: lightweight-feature\n")
    monkeypatch.setenv(CONFIG_ENV, str(path))

    assert load_config().default_composite_workflow == "lightweight-feature"


def test_a_role_pinned_in_the_config_file_beats_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "config.yaml"
    path.write_text("models:\n  implementer: pinned-model\n")
    monkeypatch.setenv(CONFIG_ENV, str(path))
    monkeypatch.setenv("SB_FAST_MODEL", "fast-model")

    config = load_config()
    assert config.models.implementer == "pinned-model"
    assert config.models.verifier == "fast-model"


def test_an_unknown_key_is_ignored_rather_than_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A setting removed from a later Switchboard must not stop the old config loading."""
    path = tmp_path / "config.yaml"
    path.write_text("communication:\n  style: whatever\nsubagents:\n  enabled: false\n")
    monkeypatch.setenv(CONFIG_ENV, str(path))

    assert load_config().subagents.enabled is False
