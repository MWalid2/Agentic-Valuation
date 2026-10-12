"""Two live wrapper calls without reading user credentials or changing .env."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from dcf_agent.config import AppConfig, ModelsConfig, Settings
from dcf_agent.llm import LocalLLM


class Choice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    level: Literal["low", "medium", "high"]


def main() -> None:
    import yaml

    root = Path(__file__).resolve().parents[1]
    with (root / "config/models.yaml").open(encoding="utf-8") as stream:
        models = ModelsConfig.model_validate(yaml.safe_load(stream))
    config = AppConfig(settings=Settings(project_root=root, mock_llm=False), models=models)
    with LocalLLM(config) as llm:
        for role in ("router", "filings"):
            result = llm.generate(
                role,
                "Return a JSON object with level set to medium.",
                Choice,
                prompt_version="setup-smoke-v1",
            )
            if result.value.level != "medium":
                raise RuntimeError("Live smoke response did not follow the instruction")
            print(
                f"{role}: model={models.roles[role].model} origin={result.origin} "
                f"attempts={result.attempts} level={result.value.level}",
                flush=True,
            )


if __name__ == "__main__":
    main()
