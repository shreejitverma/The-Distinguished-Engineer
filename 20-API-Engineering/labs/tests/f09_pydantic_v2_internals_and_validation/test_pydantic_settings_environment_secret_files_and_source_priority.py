"""pydantic-settings 2.16.2: source priority, nested delimiters, secret files, custom sources."""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

import pytest
from pydantic import AliasChoices, Field, ValidationError
from pydantic_settings import (
    BaseSettings,
    NestedSecretsSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    SettingsError,
)

from api_labs.f09_pydantic_v2_internals_and_validation.settings_sources import (
    PREFIX,
    BootstrapSettings,
    CommaListSettings,
    EnvFirstSettings,
    LlmConfig,
    MaxSplitSettings,
    NestedSettings,
    PartialDefaultSettings,
    PrecedenceSettings,
    SecretFileSettings,
    StrictDefaultSettings,
    load,
    resolve_with_provenance,
    write_kubernetes_secret_volume,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def clean_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic environment: drop every variable these settings classes could read."""
    for name in list(os.environ):
        upper = name.upper()
        if upper.startswith(PREFIX) or upper in {"LAB_UPSTREAM_TOKEN", "PYDANTIC_SETTINGS_DEBUG"}:
            monkeypatch.delenv(name)


def write_lines(path: Path, *lines: str) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.mark.unit
def test_default_priority_is_init_env_dotenv_secrets_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = write_lines(tmp_path / ".env", "TDE_LAB_LOG_LEVEL=from-dotenv")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "tde_lab_log_level").write_text("from-secret\n", encoding="utf-8")
    monkeypatch.setenv("TDE_LAB_LOG_LEVEL", "from-env")

    def level(**init: str) -> str:
        return load(PrecedenceSettings, _env_file=env_file, _secrets_dir=secrets, **init).log_level

    assert level(log_level="from-init") == "from-init"
    assert level() == "from-env"
    monkeypatch.delenv("TDE_LAB_LOG_LEVEL")
    assert level() == "from-dotenv"
    env_file.write_text("", encoding="utf-8")
    assert level() == "from-secret"
    (secrets / "tde_lab_log_level").unlink()
    assert level() == "from-default"


@pytest.mark.unit
def test_customised_order_lets_environment_beat_constructor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TDE_LAB_LOG_LEVEL", "from-env")
    assert load(PrecedenceSettings, log_level="from-init").log_level == "from-init"
    assert load(EnvFirstSettings, log_level="from-init").log_level == "from-env"
    assert load(EnvFirstSettings, region="from-init").region == "from-init"


@pytest.mark.unit
def test_json_value_and_exploded_nested_vars_merge_with_exploded_winning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    whole = {"host": "json-host", "port": 1, "user": "u", "password": "p"}
    monkeypatch.setenv("TDE_LAB_DB", json.dumps(whole))
    monkeypatch.setenv("TDE_LAB_DB__PORT", "6543")
    settings = load(NestedSettings)
    assert settings.db.host == "json-host"
    assert settings.db.port == 6543
    assert settings.db.password.get_secret_value() == "p"


@pytest.mark.unit
def test_nested_keys_deep_merge_across_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = write_lines(
        tmp_path / ".env",
        "TDE_LAB_DB__HOST=dotenv-host",
        "TDE_LAB_DB__USER=dotenv-user",
    )
    monkeypatch.setenv("TDE_LAB_DB__HOST", "env-host")
    settings = load(NestedSettings, _env_file=env_file, db={"password": "init-pw"})
    assert settings.db.host == "env-host"
    assert settings.db.user == "dotenv-user"
    assert settings.db.password.get_secret_value() == "init-pw"
    assert settings.db.port == 5432


@pytest.mark.unit
def test_partial_nested_override_discards_default_unless_partial_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TDE_LAB_DB__PORT", "6543")
    with pytest.raises(ValidationError) as excinfo:
        load(StrictDefaultSettings)
    missing = {err["loc"] for err in excinfo.value.errors() if err["type"] == "missing"}
    assert missing == {("db", "host"), ("db", "user"), ("db", "password")}

    merged = load(PartialDefaultSettings)
    assert (merged.db.host, merged.db.port, merged.db.user) == ("localhost", 6543, "app")


class UnlimitedSplitSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix=PREFIX, env_nested_delimiter="_")

    llm: LlmConfig


@pytest.mark.unit
def test_env_nested_max_split_keeps_underscored_leaf_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TDE_LAB_LLM_API_KEY", "key-EXAMPLE")
    monkeypatch.setenv("TDE_LAB_LLM_MODEL", "large")
    limited = load(MaxSplitSettings)
    assert (limited.llm.api_key, limited.llm.model) == ("key-EXAMPLE", "large")

    with pytest.raises(ValidationError) as excinfo:
        load(UnlimitedSplitSettings)
    assert [err["loc"] for err in excinfo.value.errors()] == [("llm", "api_key")]


@pytest.mark.unit
def test_complex_field_takes_json_and_rejects_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TDE_LAB_DB", '{"host": "h", "user": "u", "password": "p"}')
    monkeypatch.setenv("TDE_LAB_ALLOWED_HOSTS", '["a.example", "b.example"]')
    assert load(NestedSettings).allowed_hosts == ["a.example", "b.example"]

    monkeypatch.setenv("TDE_LAB_ALLOWED_HOSTS", "a.example,b.example")
    expected = r'field "allowed_hosts" from source "EnvSettingsSource"'
    with pytest.raises(SettingsError, match=expected):
        load(NestedSettings)


@pytest.mark.unit
def test_nodecode_hands_raw_string_to_before_validator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TDE_LAB_PORTS", "80,443")
    assert load(CommaListSettings).ports == [80, 443]


@pytest.mark.unit
def test_secret_file_needs_prefixed_name_matches_case_and_is_stripped(tmp_path: Path) -> None:
    (tmp_path / "db_password").write_text("unprefixed", encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        load(SecretFileSettings, _secrets_dir=tmp_path)
    assert [err["loc"] for err in excinfo.value.errors()] == [("db_password",)]

    (tmp_path / "tde_lab_db_password").write_text("  s3cr3t-EXAMPLE \n", encoding="utf-8")
    (tmp_path / "TDE_LAB_API_TOKEN").write_text("token-EXAMPLE\n", encoding="utf-8")
    settings = load(SecretFileSettings, _secrets_dir=tmp_path)
    assert settings.db_password.get_secret_value() == "s3cr3t-EXAMPLE"
    assert settings.api_token is not None
    assert settings.api_token.get_secret_value() == "token-EXAMPLE"
    assert "s3cr3t" not in repr(settings)


@pytest.mark.unit
def test_missing_secrets_dir_warns_and_falls_through(tmp_path: Path) -> None:
    with pytest.warns(UserWarning, match="does not exist"):
        settings = load(PrecedenceSettings, _secrets_dir=tmp_path / "absent")
    assert settings.log_level == "from-default"


@pytest.mark.unit
def test_later_secrets_dir_wins(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    for directory, value in ((first, "first"), (second, "second")):
        directory.mkdir()
        (directory / "tde_lab_region").write_text(value, encoding="utf-8")
    assert load(PrecedenceSettings, _secrets_dir=[first, second]).region == "second"
    assert load(PrecedenceSettings, _secrets_dir=[second, first]).region == "first"


@pytest.mark.unit
def test_kubernetes_atomic_swap_is_seen_only_by_new_instances(tmp_path: Path) -> None:
    volume = tmp_path / "secret-volume"
    write_kubernetes_secret_volume(
        volume, {"tde_lab_db_password": "gen-1"}, "2026_11_12_10_00_00.000000001"
    )
    before = load(SecretFileSettings, _secrets_dir=volume)
    write_kubernetes_secret_volume(
        volume, {"tde_lab_db_password": "gen-2"}, "2026_11_12_10_05_00.000000002"
    )
    after = load(SecretFileSettings, _secrets_dir=volume)
    assert (volume / "tde_lab_db_password").is_symlink()
    assert before.db_password.get_secret_value() == "gen-1"
    assert after.db_password.get_secret_value() == "gen-2"


@pytest.mark.unit
def test_unknown_dotenv_key_is_forbidden_but_unknown_env_var_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TDE_LAB_TYPO_LEVEL", "debug")
    assert load(PrecedenceSettings).log_level == "from-default"

    env_file = write_lines(tmp_path / ".env", "TDE_LAB_TYPO_LEVEL=debug")
    with pytest.raises(ValidationError) as excinfo:
        load(PrecedenceSettings, _env_file=env_file)
    (error,) = excinfo.value.errors()
    assert (error["type"], error["loc"]) == ("extra_forbidden", ("tde_lab_typo_level",))


@pytest.mark.unit
def test_dotenv_values_interpolate_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TDE_LAB_SOURCE_VALUE", "from-process")
    env_file = write_lines(
        tmp_path / ".env",
        'TDE_LAB_LOG_LEVEL="${TDE_LAB_SOURCE_VALUE}-double"',
        "TDE_LAB_REGION='${TDE_LAB_SOURCE_VALUE}-single'",
    )
    settings = load(PrecedenceSettings, _env_file=env_file)
    assert settings.log_level == "from-process-double"
    assert settings.region == "from-process-single"


class EmptyAndNoneSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=PREFIX, env_ignore_empty=True, env_parse_none_str="null"
    )

    log_level: str = "from-default"
    timeout_s: int | None = 30


@pytest.mark.unit
def test_empty_string_ignored_and_none_sentinel_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TDE_LAB_LOG_LEVEL", "")
    monkeypatch.setenv("TDE_LAB_TIMEOUT_S", "null")
    assert load(PrecedenceSettings).log_level == ""
    settings = load(EmptyAndNoneSettings)
    assert settings.log_level == "from-default"
    assert settings.timeout_s is None


class AliasSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix=PREFIX)

    upstream: str = Field(default="unset", validation_alias="LAB_UPSTREAM_TOKEN")


class AliasPrefixedSettings(AliasSettings):
    model_config = SettingsConfigDict(env_prefix_target="all")


@pytest.mark.unit
def test_env_prefix_skips_aliases_unless_targeted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TDE_LAB_TOKEN", "by-field-name")
    monkeypatch.setenv("TDE_LAB_LAB_UPSTREAM_TOKEN", "by-prefixed-alias")
    assert load(AliasSettings).upstream == "unset"
    assert load(AliasPrefixedSettings).upstream == "by-prefixed-alias"
    monkeypatch.setenv("LAB_UPSTREAM_TOKEN", "by-alias")
    assert load(AliasSettings).upstream == "by-alias"


class ChoicesSettings(BaseSettings):
    upstream: str = Field(
        default="unset", validation_alias=AliasChoices("TDE_LAB_NEW_TOKEN", "TDE_LAB_OLD_TOKEN")
    )


@pytest.mark.unit
def test_alias_choices_are_tried_in_declaration_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TDE_LAB_OLD_TOKEN", "old")
    assert load(ChoicesSettings).upstream == "old"
    monkeypatch.setenv("TDE_LAB_NEW_TOKEN", "new")
    assert load(ChoicesSettings).upstream == "new"


@pytest.mark.unit
def test_bootstrap_source_sees_only_higher_priority_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"log_level": "from-json", "region": "from-json"}), "utf-8")
    env_file = write_lines(tmp_path / ".env", "TDE_LAB_REGION=from-dotenv")

    monkeypatch.setenv("TDE_LAB_CONFIG_FILE", str(config))
    settings, owner = resolve_with_provenance(BootstrapSettings, _env_file=env_file)
    assert (settings.log_level, settings.region) == ("from-json", "from-json")
    assert owner == {
        "config_file": "EnvSettingsSource",
        "log_level": "JsonFileFromStateSource",
        "region": "JsonFileFromStateSource",
    }

    monkeypatch.delenv("TDE_LAB_CONFIG_FILE")
    late = write_lines(tmp_path / "late.env", f"TDE_LAB_CONFIG_FILE={config}")
    settings, owner = resolve_with_provenance(BootstrapSettings, _env_file=late)
    assert settings.config_file == config
    assert settings.log_level == "from-default"
    assert owner == {"config_file": "DotEnvSettingsSource"}


class NestedSecretSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix=PREFIX, secrets_nested_subdir=True)

    db: LlmConfig

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return init_settings, NestedSecretsSettingsSource(file_secret_settings)


@pytest.mark.unit
def test_nested_secrets_subdir_maps_dirs_and_skips_escaping_symlinks(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    (secrets / "tde_lab_db").mkdir(parents=True)
    (secrets / "tde_lab_db" / "api_key").write_text("key-EXAMPLE\n", encoding="utf-8")
    outside = tmp_path / "outside-model"
    outside.write_text("exfiltrated", encoding="utf-8")
    (secrets / "tde_lab_db" / "model").symlink_to(outside)

    settings = load(NestedSecretSettings, _secrets_dir=secrets)
    assert settings.db.api_key == "key-EXAMPLE"
    assert settings.db.model == "small"


class TinyNestedSecretSettings(NestedSecretSettings):
    model_config = SettingsConfigDict(secrets_dir_max_size=8)


@pytest.mark.unit
def test_nested_secrets_enforces_directory_size_cap(tmp_path: Path) -> None:
    (tmp_path / "tde_lab_db").mkdir()
    (tmp_path / "tde_lab_db" / "api_key").write_text("123456789", encoding="utf-8")
    with pytest.raises(SettingsError, match="secrets_dir size is above 8 bytes"):
        load(TinyNestedSecretSettings, _secrets_dir=tmp_path)


@pytest.mark.unit
def test_debug_logging_prints_secrets_that_the_model_masks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "tde_lab_db_password").write_text("s3cr3t-EXAMPLE", encoding="utf-8")
    monkeypatch.setenv("PYDANTIC_SETTINGS_DEBUG", "1")
    with caplog.at_level(logging.DEBUG, logger="pydantic_settings"):
        settings = load(SecretFileSettings, _secrets_dir=tmp_path)
    assert "sources in priority order" in caplog.text
    assert "s3cr3t-EXAMPLE" in caplog.text
    assert "s3cr3t-EXAMPLE" not in repr(settings)


@pytest.mark.unit
def test_settings_cached_freezes_first_read_until_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    PrecedenceSettings.settings_clear_cache()
    try:
        monkeypatch.setenv("TDE_LAB_REGION", "eu-west-1")
        first = PrecedenceSettings.settings_cached()
        monkeypatch.setenv("TDE_LAB_REGION", "ap-south-1")
        assert PrecedenceSettings.settings_cached() is first
        assert first.region == "eu-west-1"
        assert EnvFirstSettings.settings_cached() is not first
        PrecedenceSettings.settings_clear_cache()
        assert PrecedenceSettings.settings_cached().region == "ap-south-1"
    finally:
        PrecedenceSettings.settings_clear_cache()
        EnvFirstSettings.settings_clear_cache()


@pytest.mark.unit
def test_lowercase_env_name_matches_only_when_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("tde_lab_region", "lowercase-name")
    assert load(PrecedenceSettings).region == "lowercase-name"
    assert load(PrecedenceSettings, _case_sensitive=True).region == "from-default"
