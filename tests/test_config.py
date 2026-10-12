"""Isolated configuration tests; never load the user's .env."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from dcf_agent.config import ROLES, ModelsConfig, RoleConfig, Settings, load_config


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name in os.environ:
        if name.startswith("DCF_") or name in ("SEC_USER_AGENT", "FRED_API_KEY"):
            monkeypatch.delenv(name)


def write_config(root: Path) -> None:
    (root / "config").mkdir()
    payload = {"roles": {role: {"size_class": "S"} for role in ROLES}}
    (root / "config/models.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")


def test_offline_defaults_and_paths_do_not_create_storage(tmp_path: Path) -> None:
    write_config(tmp_path)
    config = load_config(tmp_path, load_env_file=False)
    assert config.settings.mock_llm is True
    assert config.settings.database_path == tmp_path / "data/valuation.sqlite3"
    assert config.settings.llm_cache_dir == tmp_path / "data/llm_cache"
    assert not (tmp_path / "data").exists()
    assert config.models.runtime.max_loaded_models == 1


def test_env_overrides_dotenv_and_secrets_are_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path)
    (tmp_path / ".env").write_text(
        "DCF_OUTPUT_DIR=from-file\nFRED_API_KEY=fixture-secret\nUNRELATED=value\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DCF_OUTPUT_DIR", "from-env")
    config = load_config(tmp_path)
    assert config.settings.output_dir == Path("from-env")
    assert config.settings.fred_api_key is not None
    assert config.settings.fred_api_key.get_secret_value() == "fixture-secret"
    assert "fixture-secret" not in repr(config)
    assert "fixture-secret" not in config.model_dump_json()


def test_dotenv_can_be_disabled(tmp_path: Path) -> None:
    write_config(tmp_path)
    (tmp_path / ".env").write_text("DCF_OUTPUT_DIR=from-file", encoding="utf-8")
    assert load_config(tmp_path, load_env_file=False).settings.output_dir == Path("outputs")


def test_live_mode_requires_selected_tags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path)
    monkeypatch.setenv("DCF_MOCK_LLM", "false")
    with pytest.raises(ValueError, match="benchmarked model tags"):
        load_config(tmp_path, load_env_file=False)


@pytest.mark.parametrize(
    "override",
    [
        {"num_ctx": 8193},
        {"num_predict": 0},
        {"thinking": True},
        {"size_class": "L"},
        {"temperature": float("nan")},
        {"model": " "},
        {"unexpected": True},
    ],
)
def test_role_limits(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RoleConfig.model_validate({"size_class": "S", **override})


@pytest.mark.parametrize(
    "runtime",
    [{"max_loaded_models": 2}, {"num_parallel": 2}, {"queue": "parallel"}, {"threads": 0}],
)
def test_single_worker_runtime(runtime: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelsConfig.model_validate(
            {"roles": {role: {"size_class": "S"} for role in ROLES}, "runtime": runtime}
        )


def test_all_roles_are_required() -> None:
    with pytest.raises(ValidationError):
        ModelsConfig.model_validate({"roles": {"router": {"size_class": "S"}}})


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://user:pass@localhost:11434",
        "http://localhost:invalid",
        "http://localhost/path",
    ],
)
def test_local_backend_only(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(ollama_url=url)


def test_missing_sec_identity_is_reported_at_use_time() -> None:
    with pytest.raises(ValueError, match="SEC_USER_AGENT"):
        Settings().require_sec_identity()


def test_real_yaml_loads_without_secrets() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root, load_env_file=False)
    assert set(config.models.roles) == set(ROLES)
