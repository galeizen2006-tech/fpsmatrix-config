# FPSMatrix Config API — versión Cloudflare Workers

Hace lo mismo que la versión Python (`../api/`) pero corre directo en
Cloudflare, sin ninguna PC prendida. Lee `config.json` del repo público
[galeizen2006-tech/fpsmatrix-config](https://github.com/galeizen2006-tech/fpsmatrix-config),
lo valida, guarda la última versión buena en Workers KV y busca cambios cada
5 minutos.

Entra en el **plan gratis** de Cloudflare (Workers, KV y Cron Triggers).

## Instalarla (para quien maneja el Cloudflare de fpsmatrix.com)

Hace falta Node.js 18 o superior (https://nodejs.org).

```bash
# 1. Dentro de esta carpeta
npm install

# 2. Entrar con la cuenta de Cloudflare donde está fpsmatrix.com
npx wrangler login

# 3. Crear el espacio donde se guarda la copia de la config
npx wrangler kv namespace create CONFIG_KV
#    -> copiá el "id" que devuelve y pegalo en wrangler.toml,
#       en lugar de PEGAR_ACA_EL_ID_DEL_KV

# 4. Crear la clave de los endpoints privados (te la pide por pantalla).
#    Usá una larga y aleatoria y guardala bien. No va en ningún archivo.
npx wrangler secret put ADMIN_API_KEY

# 5. Publicar
npx wrangler deploy
```

Al terminar, Wrangler muestra una dirección tipo
`https://fpsmatrix-config-api.<cuenta>.workers.dev`. Probá
`<esa dirección>/health`: tiene que decir `"status":"ok"`.

### Ponerle el dominio

Recomendado: **`config.fpsmatrix.com`** (no uses `api.fpsmatrix.com`, que
FPSMatrix ya reserva para otro servidor).

- Opción A: en `wrangler.toml` descomentá la línea `routes = ...` y repetí
  `npx wrangler deploy`.
- Opción B: en el panel de Cloudflare → Workers & Pages →
  `fpsmatrix-config-api` → Settings → Domains & Routes → Add → Custom domain →
  `config.fpsmatrix.com`.

Cloudflare crea solo el registro DNS y el certificado HTTPS.

## Endpoints

| Ruta | Para qué |
|---|---|
| `GET /health` | Estado: de dónde sale la config (`remote`, `cache`, `bundled`), versión y último error. |
| `GET /v1/config` | Configuración pública (sin la sección `private`). Con `ETag` / `304`. |
| `GET /v1/version?app_version=1.9.4` | Última versión, mínima soportada, si hay que actualizar. |
| `GET /v1/features?app_version=1.9.5` | Qué funciones están activas para esa versión. |
| `GET /admin/config` | Todo, incluida `private`. Header `X-Admin-Key`. |
| `POST /admin/refresh` | Baja GitHub ya mismo. Header `X-Admin-Key`. |

Ejemplos de respuestas y formato de `config.json`: ver `../api/README.md`
(son iguales en las dos versiones).

```bash
curl https://config.fpsmatrix.com/v1/version?app_version=1.9.4
curl -X POST -H "X-Admin-Key: LA_CLAVE" https://config.fpsmatrix.com/admin/refresh
```

## Cambiar la configuración

1. Editá `config.json` en la raíz del repo de GitHub.
2. **Subí `config_version` en 1.**
3. En 5 a 10 minutos la API lo toma sola (GitHub cachea unos minutos). Para
   ya: `POST /admin/refresh`.

Si el archivo tiene un error, la API lo rechaza, sigue con la versión
anterior y lo muestra en `/health` → `last_error`.

## Seguridad

- Solo HTTPS, sin redirecciones; archivos de más de 256 KB se rechazan.
- Validación estricta (`src/validate.js`): campos desconocidos, URLs `http` o de
  dominios no permitidos, versiones mal escritas, parámetros que no sean
  `true/false`, número o texto corto → se rechaza todo el archivo.
- No ejecuta nada de lo que viene de GitHub: son solo datos.
- No deja volver a un `config_version` más viejo.
- `ADMIN_API_KEY` y el token opcional son *secrets* de Cloudflare: nunca en el
  código, nunca en el repo, nunca en FPSMatrix.exe.
- Repo privado (opcional): `npx wrangler secret put FPSMATRIX_GITHUB_TOKEN`
  con un fine-grained token de solo lectura (Contents: Read-only) para ese repo.

## Probar en tu PC antes de publicar

```bash
cp .dev.vars.example .dev.vars    # poné una clave cualquiera
npm run dev                       # http://127.0.0.1:8787/health
npm test                          # 19 pruebas, sin Internet
```

Si cambiás `../config.json` y querés que también sea la config "de fábrica"
del Worker: `npm run sync-config`.
