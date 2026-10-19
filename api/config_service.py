"""Gestión de la configuración: carga, validación, caché local y recuperación.

Orden de prioridad al arrancar:
  1. cache/config.cache.json (la última config válida bajada de GitHub)
  2. config.json que viene junto a la API (valores de fábrica)
Después intenta bajar la versión de GitHub y la refresca cada
REFRESH_INTERVAL_SECONDS. Si GitHub falla o manda algo inválido, se sigue
usando la última configuración válida: nunca se queda sin configuración.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from pydantic import ValidationError

from github_service import GitHubError, GitHubService
from models import ConfigSource, FullConfig, PublicConfig, Tier, compare_versions

log = logging.getLogger("fpsmatrix.config")

BASE_DIR = Path(__file__).resolve().parent
BUNDLED_CONFIG = BASE_DIR / "config.json"
CACHE_FILE_NAME = "config.cache.json"
META_FILE_NAME = "config.meta.json"


def _env_list(name: str, default: str = "") -> list[str]:
    return [p.strip().lower() for p in os.getenv(name, default).split(",") if p.strip()]


@dataclass
class Settings:
    github_repo: str = ""
    github_branch: str = "main"
    github_config_path: str = "config.json"
    github_token: str = field(default="", repr=False)  # nunca se imprime
    refresh_interval_seconds: int = 300
    http_timeout_seconds: float = 10.0
    cache_dir: Path = BASE_DIR / "cache"
    admin_api_key: str = field(default="", repr=False)
    allowed_url_hosts: list[str] = field(default_factory=list)
    cors_origins: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(BASE_DIR / ".env")
        cache_dir = Path(os.getenv("CACHE_DIR", "./cache"))
        if not cache_dir.is_absolute():
            cache_dir = BASE_DIR / cache_dir
        return cls(
            github_repo=os.getenv("GITHUB_REPO", "").strip(),
            github_branch=os.getenv("GITHUB_BRANCH", "main").strip() or "main",
            github_config_path=os.getenv("GITHUB_CONFIG_PATH", "config.json").strip() or "config.json",
            github_token=os.getenv("FPSMATRIX_GITHUB_TOKEN", "").strip(),
            refresh_interval_seconds=max(30, int(os.getenv("REFRESH_INTERVAL_SECONDS", "300"))),
            http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "10")),
            cache_dir=cache_dir,
            admin_api_key=os.getenv("ADMIN_API_KEY", "").strip(),
            allowed_url_hosts=_env_list(
                "ALLOWED_URL_HOSTS",
                "github.com,raw.githubusercontent.com,fpsmatrix.com,www.fpsmatrix.com",
            ),
            cors_origins=[o for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()],
        )


class ConfigRejected(Exception):
    """La config descargada no pasó la validación; se conserva la anterior."""


@dataclass
class RefreshResult:
    updated: bool
    detail: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _atomic_write_json(path: Path, data: Any) -> None:
    """Escribe a un archivo temporal y lo renombra: nunca deja un JSON a medias."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class ConfigService:
    def __init__(self, settings: Settings, github: GitHubService):
        self.settings = settings
        self.github = github
        self._lock = asyncio.Lock()
        self._config: Optional[FullConfig] = None
        self.source: ConfigSource = ConfigSource.bundled
        self.etag: Optional[str] = None
        self.last_fetch_ok_at: Optional[datetime] = None
        self.last_fetch_attempt_at: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self._task: Optional[asyncio.Task] = None

    # ---------- lectura ----------

    @property
    def config(self) -> FullConfig:
        if self._config is None:
            raise RuntimeError("configuración no cargada")
        return self._config

    def public_config(self) -> PublicConfig:
        return self.config.public()

    @property
    def cache_file(self) -> Path:
        return self.settings.cache_dir / CACHE_FILE_NAME

    @property
    def meta_file(self) -> Path:
        return self.settings.cache_dir / META_FILE_NAME

    # ---------- validación ----------

    def validate(self, data: dict[str, Any]) -> FullConfig:
        try:
            return FullConfig.model_validate(
                data, context={"allowed_hosts": set(self.settings.allowed_url_hosts)}
            )
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or '(raíz)'}: {e['msg']}"
                for e in exc.errors()[:5]
            )
            raise ConfigRejected(f"config.json inválido: {problems}") from exc

    # ---------- carga inicial (sin red) ----------

    def load_initial(self) -> None:
        """Carga la mejor configuración local disponible. No usa Internet.

        Compara la copia guardada con el config.json de fábrica y se queda con
        la de config_version más alta (en empate gana la copia guardada).
        """
        best: Optional[tuple[FullConfig, ConfigSource]] = None
        for path, source in ((self.cache_file, ConfigSource.cache), (BUNDLED_CONFIG, ConfigSource.bundled)):
            if not path.exists():
                continue
            try:
                cfg = self.validate(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, ConfigRejected) as exc:
                log.warning("no se pudo usar %s: %s", path.name, exc)
                continue
            if best is None or cfg.config_version > best[0].config_version:
                best = (cfg, source)
        if best is None:
            raise RuntimeError("no hay ninguna configuración válida (ni caché ni config.json)")
        self._config, self.source = best
        if self.source is ConfigSource.cache:
            self._load_meta()
        log.info("configuración v%s cargada desde %s", self.config.config_version, self.source.value)

    def _load_meta(self) -> None:
        try:
            meta = json.loads(self.meta_file.read_text(encoding="utf-8"))
            self.etag = meta.get("etag")
            if meta.get("fetched_at"):
                self.last_fetch_ok_at = datetime.fromisoformat(meta["fetched_at"])
        except (OSError, ValueError):
            pass

    # ---------- actualización desde GitHub ----------

    async def refresh(self) -> RefreshResult:
        """Baja config.json, lo valida y, si es mejor, lo aplica y lo guarda."""
        async with self._lock:
            self.last_fetch_attempt_at = _now()
            try:
                result = await self.github.fetch_config(etag=self.etag)
                if result.not_modified:
                    self.last_fetch_ok_at = _now()
                    self.last_error = None
                    return RefreshResult(False, "sin cambios en GitHub")

                new_cfg = self.validate(result.data or {})
                current = self.config
                if new_cfg.config_version < current.config_version:
                    raise ConfigRejected(
                        f"config_version {new_cfg.config_version} es menor que la actual "
                        f"{current.config_version}; se ignora (protección contra retrocesos)"
                    )

                self.etag = result.etag
                self.last_fetch_ok_at = _now()
                self.last_error = None
                changed = new_cfg.model_dump() != current.model_dump()
                self._config = new_cfg
                self.source = ConfigSource.remote
                self._save_cache(result.data or {})
                if changed:
                    log.info("configuración actualizada a v%s", new_cfg.config_version)
                    return RefreshResult(True, f"aplicada config_version {new_cfg.config_version}")
                return RefreshResult(False, "GitHub tiene la misma configuración")
            except (GitHubError, ConfigRejected) as exc:
                self.last_error = str(exc)
                log.warning("actualización fallida, se conserva v%s: %s", self.config.config_version, exc)
                raise

    def _save_cache(self, raw: dict[str, Any]) -> None:
        try:
            _atomic_write_json(self.cache_file, raw)
            _atomic_write_json(
                self.meta_file,
                {
                    "etag": self.etag,
                    "fetched_at": self.last_fetch_ok_at.isoformat() if self.last_fetch_ok_at else None,
                    "source_url": self.github.source_url(),
                },
            )
        except OSError as exc:  # sin disco no se rompe la API, solo no hay copia
            log.error("no se pudo guardar la copia local: %s", exc)

    # ---------- actualización automática ----------

    async def _loop(self) -> None:
        while True:
            try:
                await self.refresh()
            except (GitHubError, ConfigRejected):
                pass  # ya registrado; se reintenta en el próximo ciclo
            except Exception:  # nunca dejar morir el ciclo
                log.exception("error inesperado al actualizar la configuración")
            await asyncio.sleep(self.settings.refresh_interval_seconds)

    def start(self) -> None:
        if self.github.configured and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="config-refresh")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ---------- utilidades para los endpoints ----------

    def effective_features(self, app_version: Optional[str], tier: Tier) -> dict[str, dict[str, Any]]:
        """Calcula qué funciones quedan activas para esta versión y este plan."""
        out: dict[str, dict[str, Any]] = {}
        killed = self.config.kill_switch.enabled
        for name, flag in self.config.features.items():
            enabled, reason = flag.enabled, None
            if not flag.enabled:
                reason = "apagada en la configuración"
            elif killed:
                enabled, reason = False, "kill switch activo"
            elif flag.tier is Tier.premium and tier is not Tier.premium:
                enabled, reason = False, "requiere plan premium"
            elif flag.min_app_version and (
                not app_version or compare_versions(app_version, flag.min_app_version) < 0
            ):
                enabled, reason = False, f"requiere FPSMatrix {flag.min_app_version} o superior"
            elif flag.sensitive:
                reason = "necesita que el usuario lo autorice en la app"
            out[name] = {"enabled": enabled, "sensitive": flag.sensitive, "tier": flag.tier, "reason": reason}
        return out

    def version_status(self, app_version: Optional[str]) -> dict[str, Any]:
        app = self.config.app
        info: dict[str, Any] = {
            "latest_version": app.latest_version,
            "min_supported_version": app.min_supported_version,
            "config_version": self.config.config_version,
            "download_url": app.download_url,
        }
        if app_version:
            blocked = app_version in self.config.compatibility.blocked_app_versions
            info.update(
                app_version=app_version,
                update_available=compare_versions(app_version, app.latest_version) < 0,
                supported=compare_versions(app_version, app.min_supported_version) >= 0 and not blocked,
                blocked=blocked,
            )
        return info
