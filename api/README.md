# FPSMatrix Config API

API en Python + FastAPI que sirve los parámetros de FPSMatrix a partir de un
`config.json` guardado en GitHub. Cambiás el JSON en GitHub, la API lo baja,
lo valida y la app lo recibe, **sin publicar una versión nueva de FPSMatrix**.

```
 GitHub (config.json) ──HTTPS──▶  FPSMatrix Config API  ──HTTPS──▶  FPSMatrix.exe
                                   · valida (Pydantic)               · guarda copia local
                                   · guarda la última válida         · nunca recibe tokens
                                   · funciona sin Internet
```

## Archivos

| Archivo | Qué hace |
|---|---|
| `main.py` | API principal: endpoints, errores, separación público/privado. |
| `config.json` | Parámetros de fábrica (y plantilla del archivo que va a GitHub). |
| `models.py` | Modelos Pydantic y reglas de validación. |
| `github_service.py` | Descarga `config.json` de GitHub por HTTPS (repo público o privado). |
| `config_service.py` | Carga, valida, cachea, actualiza en segundo plano y se recupera de errores. |
| `requirements.txt` | Dependencias. |
| `.env.example` | Variables de entorno de ejemplo (sin secretos). |
| `tests/test_api.py` | 20 pruebas sin Internet (GitHub simulado). |

## 1. Correrlo local (Windows, macOS o Linux)

Requiere Python 3.10 o superior.

```bash
cd fpsmatrix-config-api
python -m venv .venv
# Windows:   .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env        # en macOS/Linux: cp .env.example .env
uvicorn main:app --reload
```

Abrí http://127.0.0.1:8000/docs para probar los endpoints desde el navegador.

Sin `GITHUB_REPO` la API arranca igual y sirve el `config.json` local
(`/health` lo indica con `"github_repo_configured": false`).

Pruebas:

```bash
pytest -q
```

## 2. Crear el repositorio en GitHub (no existe todavía)

1. En GitHub: **New repository** → nombre, por ejemplo, `fpsmatrix-config`.
2. Elegí **Public** (recomendado, ver abajo) o **Private**.
3. Subí el `config.json` de esta carpeta a la raíz del repo, en la rama `main`.
   Podés borrar la sección `"private"` si el repo es público.
4. En `.env` poné `GITHUB_REPO=TU_USUARIO/fpsmatrix-config`.
5. Reiniciá la API. En `/health` vas a ver `"config_source": "remote"`.

### ¿Público o privado?

- **Público (por defecto):** se lee de `raw.githubusercontent.com` sin token.
  Es lo más simple y seguro: no hay ningún secreto que pueda filtrarse.
  Contra: cualquiera puede leer el archivo, así que **no pongas nada secreto en
  `"private"`** (en un repo público esa sección es "no mostrada por la API",
  no "secreta").
- **Privado:** creá un *fine-grained personal access token* en
  GitHub → Settings → Developer settings → Fine-grained tokens, con acceso solo
  a ese repositorio y permiso **Contents: Read-only**. Ponelo en
  `FPSMATRIX_GITHUB_TOKEN` del `.env` **del servidor**. La API pasa a leer de
  `api.github.com`. El token nunca va dentro de FPSMATRIX.exe ni sale en ninguna
  respuesta.

> El token se llama `FPSMATRIX_GITHUB_TOKEN` (y no `GITHUB_TOKEN`) a propósito:
> así la API no toma por error un token genérico que ya exista en la máquina.

Nota: `raw.githubusercontent.com` tiene caché de unos 5 minutos, así que un
cambio puede tardar ese tiempo en llegar. Para forzarlo: `POST /admin/refresh`.

## 3. Variables de entorno

| Variable | Por defecto | Para qué |
|---|---|---|
| `GITHUB_REPO` | (vacío) | `usuario/repo`. Vacío = solo config local. |
| `GITHUB_BRANCH` | `main` | Rama a leer. |
| `GITHUB_CONFIG_PATH` | `config.json` | Ruta del archivo dentro del repo. |
| `FPSMATRIX_GITHUB_TOKEN` | (vacío) | Solo para repo privado. Solo en el servidor. |
| `REFRESH_INTERVAL_SECONDS` | `300` | Cada cuánto busca cambios (mín. 30). |
| `HTTP_TIMEOUT_SECONDS` | `10` | Tiempo máximo por pedido a GitHub. |
| `CACHE_DIR` | `./cache` | Dónde guarda la última config válida. |
| `ADMIN_API_KEY` | (vacío) | Clave de `/admin/*`. Vacía = endpoints privados apagados. |
| `ALLOWED_URL_HOSTS` | github.com, raw.githubusercontent.com, fpsmatrix.com | Dominios permitidos en URLs del JSON. |
| `CORS_ORIGINS` | (vacío) | Solo si una web la llama desde el navegador. |

## 4. Endpoints

### Públicos (sin autenticación)

**`GET /health`** — estado de la API.
```json
{"status":"ok","config_source":"remote","config_version":3,
 "last_fetch_ok_at":"2026-10-19T20:00:00Z","last_fetch_attempt_at":"2026-10-19T20:00:00Z",
 "last_error":null,"github_repo_configured":true}
```
`status` es `degraded` cuando el último intento con GitHub falló; la API sigue
sirviendo la última configuración válida.

**`GET /v1/config`** — configuración pública (sin la sección `private`).
Devuelve `ETag`; si la app manda `If-None-Match` con el mismo valor recibe
`304 Not Modified` y no baja nada.

**`GET /v1/version?app_version=1.9.4`**
```json
{"latest_version":"1.9.5","min_supported_version":"1.9.0","config_version":1,
 "app_version":"1.9.4","update_available":true,"supported":true,"blocked":false,
 "download_url":"https://github.com/galeizen2006-tech/nivelgamer-releases"}
```
Acepta versiones de prueba: `1.9.5-u` < `1.9.5` < `1.9.6`.

**`GET /v1/features?app_version=1.9.5`** — qué funciones quedan activas para esa versión.
```json
{"app_version":"1.9.5","tier":"free","config_version":1,"features":{
  "benchmark":{"enabled":true,"sensitive":false,"tier":"free","reason":null},
  "game_mode_priority":{"enabled":true,"sensitive":true,"tier":"free",
     "reason":"necesita que el usuario lo autorice en la app"},
  "advanced_reports":{"enabled":false,"sensitive":false,"tier":"premium",
     "reason":"apagada en la configuración"}}}
```

### Privados (header `X-Admin-Key`)

```bash
curl -H "X-Admin-Key: TU_CLAVE" http://127.0.0.1:8000/admin/config
curl -X POST -H "X-Admin-Key: TU_CLAVE" http://127.0.0.1:8000/admin/refresh
```
`/admin/config` incluye la sección `private`. `/admin/refresh` baja GitHub ya.

### Errores

Todos los errores tienen la misma forma:
```json
{"error":{"code":"config_rejected","message":"config.json inválido: app.download_url: la URL debe usar https. Se conserva la config v3"}}
```

| Código HTTP | `code` | Cuándo |
|---|---|---|
| 401 | `unauthorized` | Falta o es incorrecta `X-Admin-Key`. |
| 404 | `not_found` | Ruta inexistente o funciones privadas apagadas. |
| 422 | `invalid_request` | Parámetro mal escrito (ej. `app_version=uno`). |
| 422 | `config_rejected` | El JSON de GitHub no pasó la validación. |
| 502 | `github_unreachable` / `github_http_error` / `github_bad_content` | Sin conexión, error de GitHub o JSON roto. |
| 503 | `github_not_configured` | `GITHUB_REPO` vacío. |

## 5. Formato de `config.json`

```jsonc
{
  "schema_version": 1,          // formato del archivo; la API rechaza otros
  "config_version": 2,          // SUBILO en cada cambio; uno menor se ignora
  "updated_at": "2026-10-19T00:00:00Z",
  "app": { "latest_version": "1.9.5", "min_supported_version": "1.9.0",
           "download_url": "https://...", "release_notes": {"es": "...", "en": "..."} },
  "compatibility": { "os": ["windows-10","windows-11"], "min_windows_build": 19041,
                     "blocked_app_versions": ["1.9.1"] },
  "kill_switch": { "enabled": false, "message": {"es": "..."} },
  "features": { "nombre": { "enabled": true, "tier": "free|premium",
                "min_app_version": "1.9.5", "sensitive": false, "description": "..." } },
  "parameters": { "fps.competitive_cap_hz": 360, "drivers.nvidia_recommended": "566.36" },
  "banners": [ { "id": "aviso-1", "level": "info", "text": {"es": "..."},
                 "url": "https://fpsmatrix.com", "min_app_version": "1.9.4" } ],
  "private": { }                // nunca sale por los endpoints públicos
}
```

**Regla de oro: cada vez que edites el archivo, subí `config_version` en 1.**

## 6. Seguridad (qué protege y cómo)

| Pedido | Cómo se cumple |
|---|---|
| Leer por HTTPS | Solo URLs `https://`; sin redirecciones; verificación de certificado. |
| Validar antes de aplicar | Pydantic con `extra="forbid"`: claves desconocidas, tipos raros, URLs `http://` o de dominios no permitidos, versiones inválidas → se rechaza todo el archivo. |
| No ejecutar código remoto | Los parámetros solo pueden ser `bool`, número o texto corto (≤500). No existe ningún campo que se ejecute. Listas, objetos o scripts dentro de `parameters` se rechazan. |
| No tocar ajustes sensibles sin permiso | Funciones con `"sensitive": true` llegan marcadas; la app debe pedir confirmación al usuario antes de aplicarlas. |
| Descargar actualizaciones solo | Tarea en segundo plano cada `REFRESH_INTERVAL_SECONDS`, con `ETag` para no bajar lo mismo. |
| Copia local sin Internet | `cache/config.cache.json`, escrito de forma atómica. Al arrancar usa la de `config_version` más alta entre caché y `config.json`. |
| Recuperarse de errores | Si GitHub falla, manda JSON roto, inválido o una versión **más vieja** (protección contra retrocesos), se conserva la última válida y `/health` muestra el error. |
| Público vs privado | `/v1/*` sin clave y sin `private`; `/admin/*` con `X-Admin-Key` (comparación en tiempo constante). Sin `ADMIN_API_KEY` los privados no existen. |
| Tokens fuera del cliente | El token (opcional) vive solo en el `.env` del servidor y nunca se devuelve ni se registra. |
| Tamaño máximo | Archivos de más de 256 KB se rechazan. |

## 7. Conectarlo con FPSMatrix

FPSMatrix ya trae un cliente para `https://api.fpsmatrix.com` (`server.go`:
kill switch, banners, driver recomendado). Esta API puede ocupar ese lugar:

1. **Publicar la API** en algún lado con HTTPS (Cloudflare Tunnel hacia la PC
   servidor, un VPS, Render, Railway, etc.) y apuntar `api.fpsmatrix.com` ahí.
   Mientras tanto, para pruebas, la app puede usar `NIVELGAMER_SERVIDOR=http://127.0.0.1:8000`.
2. **En la app (Go)**, al arrancar y cada tanto:
   - `GET /v1/version?app_version=<versión>` → si `supported` es `false`, pedir actualizar.
   - `GET /v1/config` con `If-None-Match` → guardar la respuesta en
     `%APPDATA%\FPSMatrix\config-remota.json`. Si no hay Internet, usar esa copia.
   - `GET /v1/features?app_version=<versión>` → prender/apagar funciones.
   - Si `kill_switch.enabled` → pausar lo online y mostrar el mensaje.
   - Nunca aplicar una función `sensitive` sin que el usuario acepte en pantalla.
   - Tratar `parameters` como datos: usarlos solo para claves que la app ya
     conoce y con límites propios (ej. ignorar `fps.competitive_cap_hz` fuera de 60–1000).

Ejemplo mínimo en Go:

```go
type remoteVersion struct {
    LatestVersion string `json:"latest_version"`
    Supported     *bool  `json:"supported"`
    UpdateAvail   *bool  `json:"update_available"`
}

func checkVersion(base, appVersion string) (*remoteVersion, error) {
    c := &http.Client{Timeout: 8 * time.Second}
    resp, err := c.Get(base + "/v1/version?app_version=" + url.QueryEscape(appVersion))
    if err != nil {
        return nil, err // sin Internet: seguir con la copia local
    }
    defer resp.Body.Close()
    if resp.StatusCode != http.StatusOK {
        return nil, fmt.Errorf("config api: %s", resp.Status)
    }
    var v remoteVersion
    err = json.NewDecoder(io.LimitReader(resp.Body, 64<<10)).Decode(&v)
    return &v, err
}
```

**Alternativa sin servidor:** la app puede leer el mismo `config.json` directo
de `raw.githubusercontent.com` (como ya hace con `latest.json`). Funciona, pero
entonces la app tiene que validar el archivo ella misma; con la API la
validación y la caché viven en un solo lugar.

## 8. Preparado para funciones premium

- Cada función tiene `"tier": "free" | "premium"`.
- En `main.py`, `client_tier()` hoy devuelve siempre `free` (FPSMatrix es
  100 % gratis). Para activar premium, ahí se verifica la licencia firmada
  (Ed25519, la misma que ya usa FPSMatrix) que mande la app en un header.
  Nunca se confía en un plan que la app diga sin firma.
- Siguiente paso recomendado: firmar `config.json` con Ed25519 (igual que las
  actualizaciones) para que la app pueda verificarlo aunque lo lea directo de GitHub.

## 9. Lo que NO hace (a propósito)

- No crea el repositorio ni cuentas: eso lo hacés vos (paso 2).
- No trae ninguna credencial real: `.env.example` solo tiene placeholders.
- No se despliega sola en Internet.
