"""Modelos y validación de la configuración de FPSMatrix.

Todo lo que llega de GitHub pasa por estos modelos antes de usarse.
Reglas de seguridad aplicadas aquí:
  * Campos desconocidos se rechazan (extra="forbid"): un config.json con
    claves raras o mal escritas no se aplica.
  * Los parámetros solo admiten datos simples (bool, número, texto corto).
    No hay forma de mandar código, scripts ni comandos para ejecutar.
  * Las URLs deben ser HTTPS y de un dominio permitido.
  * Las funciones marcadas como "sensitive" nunca se activan solas: el
    cliente debe pedir permiso al usuario.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Optional, Union
from urllib.parse import urlparse

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

# Versión del formato de config.json que entiende esta API.
SUPPORTED_SCHEMA_VERSION = 1

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.]+))?$")
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,63}$")
_LANG_RE = re.compile(r"^[a-z]{2}$")

MAX_TEXT = 500

Version = Annotated[str, StringConstraints(pattern=_VERSION_RE.pattern, max_length=32)]
ShortText = Annotated[str, StringConstraints(max_length=MAX_TEXT)]
# Valor de un parámetro: solo datos, nunca código.
ParamValue = Union[bool, int, float, ShortText]


def parse_version(value: str) -> tuple[int, int, int, int, str]:
    """Convierte "1.9.5" o "1.9.5-u" en una tupla comparable.

    Una versión con sufijo (build de prueba) va antes que la versión final:
    1.9.5-u < 1.9.5 < 1.9.6.
    """
    m = _VERSION_RE.match(value.strip())
    if not m:
        raise ValueError(f"versión inválida: {value!r} (se espera X.Y.Z o X.Y.Z-sufijo)")
    major, minor, patch, pre = m.groups()
    return (int(major), int(minor), int(patch), 0 if pre else 1, pre or "")


def compare_versions(a: str, b: str) -> int:
    """-1 si a < b, 0 si son iguales, 1 si a > b."""
    pa, pb = parse_version(a), parse_version(b)
    return (pa > pb) - (pa < pb)


def _check_lang_map(value: dict[str, str]) -> dict[str, str]:
    for lang in value:
        if not _LANG_RE.match(lang):
            raise ValueError(f"código de idioma inválido: {lang!r} (usar es, en, pt, ru...)")
    return value


def _check_keys(value: dict[str, Any], what: str) -> dict[str, Any]:
    for key in value:
        if not _KEY_RE.match(key):
            raise ValueError(
                f"nombre de {what} inválido: {key!r} "
                "(minúsculas, números, '.', '_' o '-', máx. 64)"
            )
    return value


def _check_https_url(url: str, info: ValidationInfo) -> str:
    """Exige HTTPS y, si el contexto trae una lista de dominios, que esté en ella."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"la URL debe usar https: {url!r}")
    allowed = (info.context or {}).get("allowed_hosts")
    if allowed and parsed.hostname.lower() not in allowed:
        raise ValueError(f"dominio no permitido en la URL: {parsed.hostname!r}")
    return url


HttpsUrl = Annotated[str, StringConstraints(max_length=300), AfterValidator(_check_https_url)]
LangText = Annotated[dict[str, ShortText], AfterValidator(_check_lang_map)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Tier(str, Enum):
    free = "free"
    premium = "premium"


class AppInfo(StrictModel):
    latest_version: Version
    min_supported_version: Version
    download_url: Optional[HttpsUrl] = None
    release_notes: LangText = Field(default_factory=dict)

    @model_validator(mode="after")
    def _min_not_above_latest(self) -> "AppInfo":
        if compare_versions(self.min_supported_version, self.latest_version) > 0:
            raise ValueError("min_supported_version no puede ser mayor que latest_version")
        return self


class Compatibility(StrictModel):
    os: list[Annotated[str, StringConstraints(max_length=32)]] = Field(default_factory=list)
    min_windows_build: Optional[int] = Field(default=None, ge=0)
    blocked_app_versions: list[Version] = Field(default_factory=list)


class KillSwitch(StrictModel):
    """Si enabled=true el cliente debe pausar las funciones online y mostrar el mensaje."""

    enabled: bool = False
    message: LangText = Field(default_factory=dict)


class FeatureFlag(StrictModel):
    enabled: bool = False
    tier: Tier = Tier.free
    min_app_version: Optional[Version] = None
    # sensitive=true: el cliente NUNCA la aplica sin que el usuario acepte.
    sensitive: bool = False
    description: ShortText = ""


class Banner(StrictModel):
    id: Annotated[str, StringConstraints(pattern=_KEY_RE.pattern)]
    level: Annotated[str, StringConstraints(pattern=r"^(info|warning)$")] = "info"
    text: LangText
    url: Optional[HttpsUrl] = None
    min_app_version: Optional[Version] = None
    max_app_version: Optional[Version] = None


class PublicConfig(StrictModel):
    """Lo que puede ver cualquiera (la app FPSMatrix, sin autenticación)."""

    schema_version: int
    config_version: int = Field(ge=1)
    updated_at: datetime
    app: AppInfo
    compatibility: Compatibility = Field(default_factory=Compatibility)
    kill_switch: KillSwitch = Field(default_factory=KillSwitch)
    features: dict[str, FeatureFlag] = Field(default_factory=dict)
    parameters: dict[str, ParamValue] = Field(default_factory=dict)
    banners: list[Banner] = Field(default_factory=list, max_length=20)

    @field_validator("schema_version")
    @classmethod
    def _schema_supported(cls, v: int) -> int:
        if v != SUPPORTED_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version {v} no soportado (esta API entiende {SUPPORTED_SCHEMA_VERSION})"
            )
        return v

    @field_validator("features")
    @classmethod
    def _feature_keys(cls, v: dict[str, FeatureFlag]) -> dict[str, FeatureFlag]:
        if len(v) > 200:
            raise ValueError("demasiadas funciones (máx. 200)")
        return _check_keys(v, "función")

    @field_validator("parameters")
    @classmethod
    def _param_keys(cls, v: dict[str, ParamValue]) -> dict[str, ParamValue]:
        if len(v) > 500:
            raise ValueError("demasiados parámetros (máx. 500)")
        return _check_keys(v, "parámetro")


class FullConfig(PublicConfig):
    """Configuración completa, con la sección privada (solo endpoints /admin)."""

    private: dict[str, ParamValue] = Field(default_factory=dict)

    @field_validator("private")
    @classmethod
    def _private_keys(cls, v: dict[str, ParamValue]) -> dict[str, ParamValue]:
        return _check_keys(v, "clave privada")

    def public(self) -> PublicConfig:
        return PublicConfig.model_validate(self.model_dump(exclude={"private"}))


# ---------- Respuestas de la API ----------


class ConfigSource(str, Enum):
    remote = "remote"      # recién descargada de GitHub
    cache = "cache"        # última válida guardada en disco
    bundled = "bundled"    # el config.json que viene con la API


class HealthResponse(BaseModel):
    status: Annotated[str, Field(description="ok | degraded")]
    config_source: ConfigSource
    config_version: int
    last_fetch_ok_at: Optional[datetime] = None
    last_fetch_attempt_at: Optional[datetime] = None
    last_error: Optional[str] = None
    github_repo_configured: bool


class VersionResponse(BaseModel):
    latest_version: str
    min_supported_version: str
    config_version: int
    app_version: Optional[str] = None
    update_available: Optional[bool] = None
    supported: Optional[bool] = None
    blocked: Optional[bool] = None
    download_url: Optional[str] = None


class EffectiveFeature(BaseModel):
    enabled: bool
    sensitive: bool
    tier: Tier
    reason: Optional[str] = None


class FeaturesResponse(BaseModel):
    app_version: Optional[str]
    tier: Tier
    config_version: int
    features: dict[str, EffectiveFeature]


class RefreshResponse(BaseModel):
    updated: bool
    config_version: int
    config_source: ConfigSource
    detail: str


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody
