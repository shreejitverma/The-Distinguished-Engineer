"""pydantic-settings 2.16.2 source priority, nested delimiters, secret files, and custom sources.

Every class here is resolved by `BaseSettings.__init__`, which builds a tuple of sources
(`settings_customise_sources` + `DefaultSettingsSource`), calls each one in order, and folds the
results with `deep_update(source_state, state)`, so an earlier source wins key by key, recursively.
"""

from __future__ import annotations

import json
import os
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

from pydantic import BaseModel, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic.fields import FieldInfo

PREFIX = "TDE_LAB_"


def load[S: BaseSettings](settings_cls: type[S], /, **overrides: Any) -> S:
    """Construct `settings_cls` with init-time overrides such as `_env_file` or `_secrets_dir`.

    Pyright synthesizes `__init__` from the fields (`dataclass_transform`), so it rejects the
    underscore arguments of `BaseSettings.__init__` and flags env-supplied required fields as
    missing; unpacking a `dict[str, Any]` is the type-checker-neutral way to call it.
    """
    return settings_cls(**overrides)


class PrecedenceSettings(BaseSettings):
    """Two scalar fields, readable from every built-in source."""

    model_config = SettingsConfigDict(env_prefix=PREFIX)

    log_level: str = "from-default"
    region: str = "from-default"


class EnvFirstSettings(PrecedenceSettings):
    """Operators beat code: environment variables outrank constructor kwargs."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return env_settings, init_settings, dotenv_settings, file_secret_settings


class DatabaseConfig(BaseModel):
    host: str
    port: int = 5432
    user: str
    password: SecretStr


class NestedSettings(BaseSettings):
    """`TDE_LAB_DB__HOST=x` -> `{"db": {"host": "x"}}` via `env_nested_delimiter`."""

    model_config = SettingsConfigDict(env_prefix=PREFIX, env_nested_delimiter="__")

    db: DatabaseConfig
    allowed_hosts: list[str] = []


_DEFAULT_DB = DatabaseConfig(host="localhost", user="app", password=SecretStr("dev-only"))


class StrictDefaultSettings(BaseSettings):
    """A nested default is replaced wholesale when any source supplies part of it."""

    model_config = SettingsConfigDict(env_prefix=PREFIX, env_nested_delimiter="__")

    db: DatabaseConfig = _DEFAULT_DB


class PartialDefaultSettings(StrictDefaultSettings):
    """The nested default is dumped into the merge, so a partial override keeps its other keys."""

    model_config = SettingsConfigDict(nested_model_default_partial_update=True)


class LlmConfig(BaseModel):
    api_key: str
    model: str = "small"


class MaxSplitSettings(BaseSettings):
    """A single-character delimiter that also appears inside field names needs a split limit."""

    model_config = SettingsConfigDict(
        env_prefix=PREFIX, env_nested_delimiter="_", env_nested_max_split=1
    )

    llm: LlmConfig


class CommaListSettings(BaseSettings):
    """`NoDecode` skips the JSON decode so a validator can parse a comma-separated value."""

    model_config = SettingsConfigDict(env_prefix=PREFIX)

    ports: Annotated[list[int], NoDecode] = []

    @field_validator("ports", mode="before")
    @classmethod
    def split_commas(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in value.split(",") if part.strip()]
        return value


class SecretFileSettings(BaseSettings):
    """Secret files are looked up under `<secrets_dir>/<env_prefix><field_name>`."""

    model_config = SettingsConfigDict(env_prefix=PREFIX)

    db_password: SecretStr
    api_token: SecretStr | None = None


_PROVENANCE: ContextVar[dict[str, dict[str, Any]] | None] = ContextVar(
    "settings_provenance", default=None
)


class ProvenanceProbe(PydanticBaseSettingsSource):
    """Contributes nothing; snapshots what every higher-priority source returned."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        _PROVENANCE.set({name: dict(data) for name, data in self.settings_sources_data.items()})
        return {}


class JsonFileFromStateSource(PydanticBaseSettingsSource):
    """Bootstrap source: loads the JSON file named by `config_file` in the higher-priority state."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        config_file = self.current_state.get("config_file")
        if config_file is None:
            return {}
        raw: object = json.loads(Path(str(config_file)).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError(f"{config_file} must hold a JSON object")
        document = cast("dict[str, Any]", raw)
        return {name: document[name] for name in self.settings_cls.model_fields if name in document}


class BootstrapSettings(BaseSettings):
    """init -> env -> JSON file named by an earlier source -> .env -> secrets -> defaults."""

    model_config = SettingsConfigDict(env_prefix=PREFIX)

    config_file: Path | None = None
    log_level: str = "from-default"
    region: str = "from-default"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            JsonFileFromStateSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
            ProvenanceProbe(settings_cls),
        )


def resolve_with_provenance(
    settings_cls: type[BootstrapSettings], **kwargs: Any
) -> tuple[BootstrapSettings, dict[str, str]]:
    """Build the settings and report, per top-level key, the highest-priority source that set it."""
    token = _PROVENANCE.set(None)
    try:
        settings = settings_cls(**kwargs)
        states = _PROVENANCE.get()
    finally:
        _PROVENANCE.reset(token)
    if states is None:
        raise RuntimeError("ProvenanceProbe did not run; is it in settings_customise_sources?")
    return settings, field_provenance(states)


def field_provenance(states: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """`states` is in priority order (highest first), so the first source to name a key owns it."""
    owner: dict[str, str] = {}
    for source_name, data in states.items():
        for key in data:
            owner.setdefault(key, source_name)
    return owner


def write_kubernetes_secret_volume(root: Path, data: Mapping[str, str], generation: str) -> None:
    """Mimic the kubelet `AtomicWriter` layout: `key -> ..data/key`, `..data -> ..<generation>`.

    The new payload is written to a fresh timestamped directory, a `..data_tmp` symlink is pointed
    at it, and `os.replace` renames that symlink over `..data` in one atomic `rename(2)`.
    """
    root.mkdir(parents=True, exist_ok=True)
    payload_dir = root / f"..{generation}"
    payload_dir.mkdir()
    for key, value in data.items():
        (payload_dir / key).write_text(value, encoding="utf-8")
    staging = root / "..data_tmp"
    staging.symlink_to(payload_dir.name)
    os.replace(staging, root / "..data")
    for key in data:
        visible = root / key
        if not visible.is_symlink():
            visible.symlink_to(Path("..data") / key)
