"""Conexión con GitHub: descarga config.json por HTTPS.

* Repo público: se lee de raw.githubusercontent.com, sin token.
* Repo privado: se lee de la API de GitHub con un token que vive SOLO en
  el servidor (variable de entorno FPSMATRIX_GITHUB_TOKEN). El token nunca se envía
  a la app FPSMatrix ni aparece en ninguna respuesta ni en los logs.

Este módulo solo descarga bytes y los convierte en JSON. No valida el
contenido (eso es tarea de config_service + models) y nunca ejecuta nada.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx

log = logging.getLogger("fpsmatrix.github")

RAW_BASE = "https://raw.githubusercontent.com"
API_BASE = "https://api.github.com"
MAX_BYTES = 256 * 1024  # un config.json razonable pesa unos pocos KB

_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9_.\-/]+$")
_PATH_RE = re.compile(r"^[A-Za-z0-9_.\-/]+\.json$")


class GitHubError(Exception):
    """Error base al hablar con GitHub. El mensaje es seguro para mostrar."""

    code = "github_error"


class GitHubNotConfigured(GitHubError):
    code = "github_not_configured"


class GitHubConnectionError(GitHubError):
    code = "github_unreachable"


class GitHubHTTPError(GitHubError):
    code = "github_http_error"

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class GitHubContentError(GitHubError):
    code = "github_bad_content"


@dataclass
class FetchResult:
    data: Optional[dict[str, Any]]  # None si GitHub respondió 304 (sin cambios)
    etag: Optional[str]
    not_modified: bool = False


class GitHubService:
    def __init__(
        self,
        repo: str,
        branch: str = "main",
        path: str = "config.json",
        token: Optional[str] = None,
        timeout: float = 10.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.repo = (repo or "").strip()
        self.branch = (branch or "main").strip()
        self.path = (path or "config.json").strip().lstrip("/")
        self._token = (token or "").strip() or None
        self.timeout = timeout
        self._transport = transport  # las pruebas inyectan un transporte falso

        if self.repo and not _REPO_RE.match(self.repo):
            raise ValueError("GITHUB_REPO debe tener el formato usuario/repositorio")
        if not _BRANCH_RE.match(self.branch) or ".." in self.branch:
            raise ValueError("GITHUB_BRANCH inválido")
        if not _PATH_RE.match(self.path) or ".." in self.path:
            raise ValueError("GITHUB_CONFIG_PATH debe ser una ruta a un archivo .json")

    @property
    def configured(self) -> bool:
        return bool(self.repo)

    @property
    def uses_token(self) -> bool:
        return self._token is not None

    def source_url(self) -> str:
        """URL que se consulta. Nunca incluye el token."""
        if self.uses_token:
            return f"{API_BASE}/repos/{self.repo}/contents/{self.path}?ref={self.branch}"
        return f"{RAW_BASE}/{self.repo}/{self.branch}/{self.path}"

    def _headers(self, etag: Optional[str]) -> dict[str, str]:
        headers = {"User-Agent": "FPSMatrix-Config-API/1.0"}
        if self.uses_token:
            headers["Authorization"] = f"Bearer {self._token}"
            headers["Accept"] = "application/vnd.github.raw+json"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        if etag:
            headers["If-None-Match"] = etag
        return headers

    async def fetch_config(self, etag: Optional[str] = None) -> FetchResult:
        if not self.configured:
            raise GitHubNotConfigured("GITHUB_REPO no está configurado; se usa la copia local")

        url = self.source_url()
        if not url.startswith("https://"):
            raise GitHubError("solo se permite HTTPS")

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self._transport,
                follow_redirects=False,
                verify=True,
            ) as client:
                async with client.stream("GET", url, headers=self._headers(etag)) as resp:
                    if resp.status_code == 304:
                        return FetchResult(data=None, etag=etag, not_modified=True)
                    if resp.status_code != 200:
                        raise GitHubHTTPError(resp.status_code, _http_message(resp.status_code))
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            raise GitHubContentError(
                                f"config.json supera el máximo de {MAX_BYTES // 1024} KB"
                            )
                    new_etag = resp.headers.get("ETag")
        except httpx.TimeoutException as exc:
            raise GitHubConnectionError("GitHub no respondió a tiempo") from exc
        except httpx.TransportError as exc:
            raise GitHubConnectionError(f"no se pudo conectar con GitHub ({type(exc).__name__})") from exc

        try:
            data = json.loads(bytes(body).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubContentError(f"config.json no es JSON válido: {exc}") from exc
        if not isinstance(data, dict):
            raise GitHubContentError("config.json debe ser un objeto JSON")
        return FetchResult(data=data, etag=new_etag)


def _http_message(status: int) -> str:
    if status == 401:
        return "GitHub rechazó el token (401). Revisá FPSMATRIX_GITHUB_TOKEN"
    if status == 403:
        return "GitHub negó el acceso o hay límite de pedidos (403)"
    if status == 404:
        return "No se encontró el repositorio, la rama o el archivo (404). Si el repo es privado hace falta FPSMATRIX_GITHUB_TOKEN"
    if status >= 500:
        return f"GitHub tuvo un error interno ({status})"
    return f"GitHub respondió {status}"
