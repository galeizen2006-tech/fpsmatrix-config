# fpsmatrix-config

Configuración remota de FPSMatrix.

- `config.json` (en la raíz): el archivo que lee la API. **Editalo acá y subí
  `config_version` en 1 cada vez**; la API lo baja sola y lo valida antes de usarlo.
- `api/`: el código de FPSMatrix Config API (Python + FastAPI). Instrucciones
  completas en [api/README.md](api/README.md).

Este repo es público: no pongas contraseñas, tokens ni nada secreto en ningún archivo.
