"""FPSMatrix Config API - API principal.

Endpoints públicos (los usa la app FPSMatrix, sin token):
  GET /health                     estado de la API y de la configuración
  GET /v1/config                  configuración pública actual
  GET /v1/version?app_version=    última versión, mínima soportada, ¿hay que actualizar?
  GET /v1/features?app_version=   funciones activas para esa versión

Endpoints privados (header X-Admin-Key = ADMIN_API_KEY):
  GET  /admin/config              configuración completa (incluye "private")
  POST /admin/refresh             fuerza la descarga desde GitHub ahora

Ejecutar:  uvicorn main:app --reload
Documentación interactiva:  http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from config_service import ConfigRejected, ConfigService, Settings
from github_service import GitHubError, GitHubNotConfigured, GitHubService
from models import (
    ErrorResponse,
    FeaturesResponse,
    FullConfig,
    HealthResponse,
    PublicConfig,
    RefreshResponse,
    Tier,
    VersionResponse,
    parse_version,
)

API_VERSION = "1.0.0"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def _check_version_param(app_version: Optional[str]) -> Optional[str]:
    if app_version is None:
        return None
    try:
        parse_version(app_version)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return app_version


def create_app(
    settings: Optional[Settings] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    auto_refresh: bool = True,
) -> FastAPI:
    """Crea la app. Las pruebas pasan su propia configuración y un GitHub falso."""
    settings = settings or Settings.from_env()
    github = GitHubService(
        repo=settings.github_repo,
        branch=settings.github_branch,
        path=settings.github_config_path,
        token=settings.github_token,
        timeout=settings.http_timeout_seconds,
        transport=transport,
    )
    service = ConfigService(settings, github)
    service.load_initial()  # siempre arranca con algo válido, aunque no haya Internet

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if auto_refresh:
            service.start()
        yield
        await service.stop()

    app = FastAPI(
        title="FPSMatrix Config API",
        version=API_VERSION,
        description="Parámetros de FPSMatrix gestionados desde un config.json en GitHub.",
        lifespan=lifespan,
        responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    )
    app.state.service = service

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET"],
            allow_headers=["*"],
        )

    # ---------- manejo de errores uniforme ----------

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(_: Request, exc: StarletteHTTPException):
        codes = {401: "unauthorized", 403: "forbidden", 404: "not_found", 422: "invalid_request", 503: "unavailable"}
        return _error(exc.status_code, codes.get(exc.status_code, "error"), str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(_: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {"loc": [], "msg": "pedido inválido"}
        where = ".".join(str(p) for p in first["loc"])
        return _error(422, "invalid_request", f"{where}: {first['msg']}")

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        logging.getLogger("fpsmatrix.api").exception("error no controlado")
        return _error(500, "internal_error", "Error interno. La configuración anterior sigue activa.")

    # ---------- dependencias ----------

    def get_service() -> ConfigService:
        return app.state.service

    def require_admin(x_admin_key: Optional[str] = Header(default=None)) -> None:
        """Protege las funciones privadas. Sin ADMIN_API_KEY quedan apagadas."""
        if not settings.admin_api_key:
            raise HTTPException(status_code=404, detail="Funciones privadas desactivadas (falta ADMIN_API_KEY)")
        if not x_admin_key or not secrets.compare_digest(x_admin_key, settings.admin_api_key):
            raise HTTPException(status_code=401, detail="X-Admin-Key ausente o incorrecta")

    def client_tier() -> Tier:
        """Plan del cliente. Hoy todo FPSMatrix es gratis, así que siempre es 'free'.

        Punto de extensión para premium: acá se verificaría la licencia firmada
        (Ed25519) que manda la app. Nunca se confía en un "tier" que el cliente
        diga por su cuenta.
        """
        return Tier.free

    # ---------- endpoints públicos ----------

    @app.get("/health", response_model=HealthResponse, tags=["público"])
    async def health(svc: ConfigService = Depends(get_service)):
        degraded = svc.github.configured and svc.last_error is not None
        return HealthResponse(
            status="degraded" if degraded else "ok",
            config_source=svc.source,
            config_version=svc.config.config_version,
            last_fetch_ok_at=svc.last_fetch_ok_at,
            last_fetch_attempt_at=svc.last_fetch_attempt_at,
            last_error=svc.last_error,
            github_repo_configured=svc.github.configured,
        )

    @app.get("/v1/config", response_model=PublicConfig, tags=["público"])
    async def get_config(request: Request, svc: ConfigService = Depends(get_service)):
        public = svc.public_config()
        body = public.model_dump_json()
        etag = '"' + hashlib.sha256(body.encode()).hexdigest()[:32] + '"'
        headers = {"ETag": etag, "Cache-Control": "public, max-age=60", "X-Config-Source": svc.source.value}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(content=body, media_type="application/json", headers=headers)

    @app.get("/v1/version", response_model=VersionResponse, tags=["público"])
    async def get_version(
        app_version: Optional[str] = Query(default=None, examples=["1.9.5"], description="Versión instalada"),
        svc: ConfigService = Depends(get_service),
    ):
        return VersionResponse(**svc.version_status(_check_version_param(app_version)))

    @app.get("/v1/features", response_model=FeaturesResponse, tags=["público"])
    async def get_features(
        app_version: Optional[str] = Query(default=None, examples=["1.9.5"]),
        tier: Tier = Depends(client_tier),
        svc: ConfigService = Depends(get_service),
    ):
        app_version = _check_version_param(app_version)
        return FeaturesResponse(
            app_version=app_version,
            tier=tier,
            config_version=svc.config.config_version,
            features=svc.effective_features(app_version, tier),
        )

    # ---------- endpoints privados ----------

    @app.get("/admin/config", response_model=FullConfig, tags=["privado"], dependencies=[Depends(require_admin)])
    async def admin_config(svc: ConfigService = Depends(get_service)):
        return svc.config

    @app.post("/admin/refresh", response_model=RefreshResponse, tags=["privado"], dependencies=[Depends(require_admin)])
    async def admin_refresh(svc: ConfigService = Depends(get_service)):
        try:
            result = await svc.refresh()
        except GitHubNotConfigured as exc:
            return _error(503, exc.code, str(exc))
        except GitHubError as exc:
            return _error(502, exc.code, f"{exc}. Se conserva la config v{svc.config.config_version}")
        except ConfigRejected as exc:
            return _error(422, "config_rejected", f"{exc}. Se conserva la config v{svc.config.config_version}")
        return RefreshResponse(
            updated=result.updated,
            config_version=svc.config.config_version,
            config_source=svc.source,
            detail=result.detail,
        )

    return app


app = create_app()
