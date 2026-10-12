"""Typed local configuration; loading performs no network or filesystem writes."""

from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Role = Literal[
    "router", "data", "filings", "normalizer", "forecast", "wacc", "comps", "critic", "reporter"
]
ROLES: tuple[Role, ...] = (
    "router",
    "data",
    "filings",
    "normalizer",
    "forecast",
    "wacc",
    "comps",
    "critic",
    "reporter",
)


class RoleConfig(BaseModel):
    """Model tags remain unset until local benchmarks select them."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    model: str | None = Field(default=None, min_length=1, pattern=r"\S")
    size_class: Literal["S", "M"]
    num_ctx: int = Field(default=4096, ge=512, le=8192)
    num_predict: int = Field(default=250, ge=1, le=512)
    temperature: float = Field(default=0.0, ge=0, le=1, allow_inf_nan=False)
    thinking: Literal[False] = False


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    threads: int | None = Field(default=None, ge=1)
    queue: Literal["single_worker"] = "single_worker"
    max_loaded_models: Literal[1] = 1
    num_parallel: Literal[1] = 1
    response_cache: bool = True
    timeout_seconds: float = Field(default=180.0, gt=0, allow_inf_nan=False)


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    roles: dict[Role, RoleConfig]
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

    @model_validator(mode="after")
    def validate_roles(self) -> Self:
        if set(self.roles) != set(ROLES):
            raise ValueError("Model configuration must define all nine roles")
        return self


class Settings(BaseSettings):
    """Environment settings. Optional credentials permit offline development."""

    model_config = SettingsConfigDict(
        env_prefix="DCF_",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
        env_ignore_empty=True,
    )
    project_root: Path = Field(default_factory=Path.cwd)
    data_dir: Path = Path("data")
    output_dir: Path = Path("outputs")
    models_file: Path = Path("config/models.yaml")
    ollama_url: str = "http://127.0.0.1:11434"
    mock_llm: bool = True
    sec_user_agent: SecretStr | None = Field(default=None, validation_alias="SEC_USER_AGENT")
    fred_api_key: SecretStr | None = Field(default=None, validation_alias="FRED_API_KEY")

    @field_validator("ollama_url")
    @classmethod
    def validate_local_server(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme not in ("http", "https")
            or parsed.hostname not in ("localhost", "127.0.0.1", "::1")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("Ollama URL must be a local HTTP(S) origin without credentials")
        try:
            _ = parsed.port
        except ValueError as error:
            raise ValueError("Invalid Ollama port") from error
        return value.rstrip("/")

    def resolve_path(self, path: Path) -> Path:
        return (path if path.is_absolute() else self.project_root / path).resolve()

    @property
    def database_path(self) -> Path:
        return self.resolve_path(self.data_dir) / "valuation.sqlite3"

    @property
    def raw_cache_dir(self) -> Path:
        return self.resolve_path(self.data_dir) / "raw"

    @property
    def llm_cache_dir(self) -> Path:
        return self.resolve_path(self.data_dir) / "llm_cache"

    def require_sec_identity(self) -> str:
        """Check presence now; the adapter will check SEC contact requirements."""
        if self.sec_user_agent is None or not self.sec_user_agent.get_secret_value().strip():
            raise ValueError("Set SEC_USER_AGENT before requesting EDGAR data")
        return self.sec_user_agent.get_secret_value()


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    settings: Settings
    models: ModelsConfig

    def require_live_models(self) -> None:
        missing = [role for role, config in self.models.roles.items() if config.model is None]
        if missing:
            raise ValueError("Select benchmarked model tags for roles: " + ", ".join(missing))


def load_config(project_root: Path | None = None, *, load_env_file: bool = True) -> AppConfig:
    """Environment overrides .env; relative paths are anchored to project_root."""
    root = (project_root or Path.cwd()).resolve()
    # Pydantic's mypy plugin omits BaseSettings' runtime _env_file option.
    settings = Settings(  # type: ignore[call-arg]
        _env_file=root / ".env" if load_env_file else None, project_root=root
    )
    path = settings.resolve_path(settings.models_file)
    with path.open(encoding="utf-8") as stream:
        payload = yaml.safe_load(stream)
    models = ModelsConfig.model_validate(payload)
    config = AppConfig(settings=settings, models=models)
    if not settings.mock_llm:
        config.require_live_models()
    return config
