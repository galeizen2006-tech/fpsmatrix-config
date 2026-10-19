# fpsmatrix-config

Configuración remota de FPSMatrix.

- `config.json` (en la raíz): el archivo que lee la API. **Editalo acá y subí
  `config_version` en 1 cada vez**; la API lo baja sola y lo valida antes de usarlo.
- `worker/`: la API para **Cloudflare Workers** (recomendada: corre en
  Cloudflare sin PC prendida). Pasos en [worker/README.md](worker/README.md).
- `api/`: la misma API en Python + FastAPI, para correr en una PC o servidor.
  Pasos en [api/README.md](api/README.md).

Este repo es público: no pongas contraseñas, tokens ni nada secreto en ningún archivo.
