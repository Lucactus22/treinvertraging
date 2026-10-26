"""Settings: environment variables plus an optional ``settings.yaml`` in CONFIG_DIR.

Precedence (highest first): init kwargs, environment variables / ``.env``, ``settings.yaml``,
defaults. Environment variable names equal the field names (case-insensitive).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import Field, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

ClaimMode = Literal["dry_run", "approve", "auto"]


class _YamlSettingsSource(PydanticBaseSettingsSource):
    """Loads ``<CONFIG_DIR>/settings.yaml`` (keys are the lower-case field names)."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._data = self._load()

    def _config_dir(self) -> Path:
        env = EnvSettingsSource(self.settings_cls)
        raw = env.env_vars.get("config_dir")
        return Path(raw) if raw else Path("/config")

    def _load(self) -> dict[str, Any]:
        path = self._config_dir() / "settings.yaml"
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k).lower(): v for k, v in data.items() if v is not None}

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name.lower()), field_name, False

    def __call__(self) -> dict[str, Any]:
        return {
            name: self._data[name.lower()]
            for name in self.settings_cls.model_fields
            if name.lower() in self._data
        }


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    DATA_DIR: Path = Path("/data")
    CONFIG_DIR: Path = Path("/config")
    CLAIM_MODE: ClaimMode = "approve"
    AUTO_AFTER_N_APPROVED: int = 5
    TIMEZONE: str = "Europe/Amsterdam"
    NS_API_KEY: str = ""
    APPRISE_URLS: str = ""  # comma/space separated Apprise URLs
    WEB_USERNAME: str = "admin"
    WEB_PASSWORD: str = ""
    PUBLIC_URL: str = "http://localhost:8080"
    HEADLESS: bool = False
    RUN_WINDOW: str = "07:00-22:00"
    # Comma separated in env (e.g. "gtbv,mijnns"); a YAML list also works.
    SUBMIT_CHANNELS: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["gtbv", "mijnns"])

    # Station codes always polled by the NS API arrivals archiver, on top of the top-N destinations.
    ARCHIVE_EXTRA_STATIONS: Annotated[list[str], NoDecode] = Field(default_factory=list)
    ARCHIVE_TOP_N: int = 10

    @field_validator("SUBMIT_CHANNELS", "ARCHIVE_EXTRA_STATIONS", mode="before")
    @classmethod
    def _split_channels(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [c for c in v.replace(",", " ").split() if c]
        return v

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
            dotenv_settings,
            _YamlSettingsSource(settings_cls),
            file_secret_settings,
        )

    @property
    def db_path(self) -> Path:
        return self.DATA_DIR / "app.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def apprise_urls(self) -> list[str]:
        return [u for u in self.APPRISE_URLS.replace(",", " ").split() if u]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """For tests: forget the cached Settings (and re-read env on next ``get_settings()``)."""
    get_settings.cache_clear()
