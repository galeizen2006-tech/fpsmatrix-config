// FPSMatrix Config API - versión Cloudflare Workers.
//
// Mismos endpoints que la versión Python (api/):
//   GET  /health                     estado de la API y de la configuración
//   GET  /v1/config                  configuración pública (sin "private"), con ETag
//   GET  /v1/version?app_version=    última versión, mínima soportada, ¿hay que actualizar?
//   GET  /v1/features?app_version=   funciones activas para esa versión
//   GET  /admin/config               configuración completa      (header X-Admin-Key)
//   POST /admin/refresh              baja GitHub ahora            (header X-Admin-Key)
//
// La última configuración válida se guarda en Workers KV (CONFIG_KV), así que
// si GitHub falla se sigue sirviendo esa. Un Cron Trigger la actualiza sola.

import DEFAULT_CONFIG from "./default-config.js";
import { ConfigRejected, compareVersions, parseVersion, validateConfig } from "./validate.js";

const API_VERSION = "1.0.0";
const MAX_BYTES = 256 * 1024;
const KV_CONFIG = "config";
const KV_META = "meta";

class HttpError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

// ---------- ajustes ----------

function settings(env) {
  const list = (s, d) => (s ?? d).split(",").map((x) => x.trim().toLowerCase()).filter(Boolean);
  const repo = (env.GITHUB_REPO || "").trim();
  const branch = (env.GITHUB_BRANCH || "main").trim();
  const path = (env.GITHUB_CONFIG_PATH || "config.json").trim().replace(/^\/+/, "");
  if (repo && !/^[A-Za-z0-9_.\-]+\/[A-Za-z0-9_.\-]+$/.test(repo)) throw new Error("GITHUB_REPO inválido");
  if (!/^[A-Za-z0-9_.\-\/]+$/.test(branch) || branch.includes("..")) throw new Error("GITHUB_BRANCH inválido");
  if (!/^[A-Za-z0-9_.\-\/]+\.json$/.test(path) || path.includes("..")) throw new Error("GITHUB_CONFIG_PATH inválido");
  return {
    repo,
    branch,
    path,
    token: (env.FPSMATRIX_GITHUB_TOKEN || "").trim(), // secreto opcional, solo para repo privado
    adminKey: (env.ADMIN_API_KEY || "").trim(),
    interval: Math.max(60, Number(env.REFRESH_INTERVAL_SECONDS || 300)),
    allowedHosts: list(env.ALLOWED_URL_HOSTS, "github.com,raw.githubusercontent.com,fpsmatrix.com,www.fpsmatrix.com"),
    kv: env.CONFIG_KV || null,
  };
}

// ---------- estado: KV + configuración de fábrica ----------

async function loadState(s) {
  let meta = {};
  let best = { config: validateConfig(DEFAULT_CONFIG, s.allowedHosts), source: "bundled" };
  if (s.kv) {
    const [raw, metaRaw] = await Promise.all([s.kv.get(KV_CONFIG), s.kv.get(KV_META)]);
    try {
      meta = metaRaw ? JSON.parse(metaRaw) : {};
    } catch {
      meta = {};
    }
    if (raw) {
      try {
        const cached = validateConfig(JSON.parse(raw), s.allowedHosts);
        if (cached.config_version >= best.config.config_version) best = { config: cached, source: meta.source || "cache" };
      } catch (e) {
        console.warn("copia en KV inválida, se ignora:", e.message);
      }
    }
  }
  return { ...best, meta };
}

async function saveMeta(s, meta) {
  if (s.kv) await s.kv.put(KV_META, JSON.stringify(meta));
}

// ---------- GitHub ----------

function sourceUrl(s) {
  return s.token
    ? `https://api.github.com/repos/${s.repo}/contents/${s.path}?ref=${encodeURIComponent(s.branch)}`
    : `https://raw.githubusercontent.com/${s.repo}/${s.branch}/${s.path}`;
}

function httpMessage(status) {
  if (status === 401) return "GitHub rechazó el token (401). Revisá FPSMATRIX_GITHUB_TOKEN";
  if (status === 403) return "GitHub negó el acceso o hay límite de pedidos (403)";
  if (status === 404) return "No se encontró el repositorio, la rama o el archivo (404). Si el repo es privado hace falta FPSMATRIX_GITHUB_TOKEN";
  if (status >= 500) return `GitHub tuvo un error interno (${status})`;
  return `GitHub respondió ${status}`;
}

async function fetchGitHub(s, etag) {
  if (!s.repo) throw new HttpError(503, "github_not_configured", "GITHUB_REPO no está configurado; se usa la copia local");
  const headers = { "User-Agent": "FPSMatrix-Config-API/1.0" };
  if (s.token) {
    headers.Authorization = `Bearer ${s.token}`;
    headers.Accept = "application/vnd.github.raw+json";
    headers["X-GitHub-Api-Version"] = "2022-11-28";
  }
  if (etag) headers["If-None-Match"] = etag;

  let resp;
  try {
    resp = await fetch(sourceUrl(s), { headers, redirect: "manual", signal: AbortSignal.timeout(10000) });
  } catch (e) {
    throw new HttpError(502, "github_unreachable", `no se pudo conectar con GitHub (${e.name || "error"})`);
  }
  if (resp.status === 304) return { notModified: true, etag };
  if (resp.status !== 200) throw new HttpError(502, "github_http_error", httpMessage(resp.status));
  const len = Number(resp.headers.get("content-length") || 0);
  if (len > MAX_BYTES) throw new HttpError(502, "github_bad_content", "config.json supera el máximo de 256 KB");
  const text = await resp.text();
  if (text.length > MAX_BYTES) throw new HttpError(502, "github_bad_content", "config.json supera el máximo de 256 KB");
  let data;
  try {
    data = JSON.parse(text);
  } catch (e) {
    throw new HttpError(502, "github_bad_content", `config.json no es JSON válido: ${e.message}`);
  }
  if (data === null || typeof data !== "object" || Array.isArray(data))
    throw new HttpError(502, "github_bad_content", "config.json debe ser un objeto JSON");
  return { data, etag: resp.headers.get("ETag") };
}

// Baja, valida y guarda. Si algo falla, conserva la última válida y lo anota.
export async function refresh(s, state) {
  const meta = { ...state.meta, last_fetch_attempt_at: new Date().toISOString() };
  try {
    const res = await fetchGitHub(s, meta.etag);
    if (res.notModified) {
      Object.assign(meta, { last_fetch_ok_at: new Date().toISOString(), last_error: null });
      await saveMeta(s, meta);
      return { updated: false, detail: "sin cambios en GitHub", state: { ...state, meta } };
    }
    let cfg;
    try {
      cfg = validateConfig(res.data, s.allowedHosts);
    } catch (e) {
      if (e instanceof ConfigRejected) throw new HttpError(422, "config_rejected", e.message);
      throw e;
    }
    if (cfg.config_version < state.config.config_version)
      throw new HttpError(422, "config_rejected",
        `config_version ${cfg.config_version} es menor que la actual ${state.config.config_version}; se ignora (protección contra retrocesos)`);

    const changed = JSON.stringify(cfg) !== JSON.stringify(state.config);
    Object.assign(meta, { etag: res.etag, last_fetch_ok_at: new Date().toISOString(), last_error: null, source: "remote" });
    if (s.kv && (changed || state.source !== "remote")) await s.kv.put(KV_CONFIG, JSON.stringify(res.data));
    await saveMeta(s, meta);
    return {
      updated: changed,
      detail: changed ? `aplicada config_version ${cfg.config_version}` : "GitHub tiene la misma configuración",
      state: { config: cfg, source: "remote", meta },
    };
  } catch (e) {
    meta.last_error = e.message;
    await saveMeta(s, meta).catch(() => {});
    e.state = { ...state, meta };
    throw e;
  }
}

// ---------- utilidades ----------

function json(body, status = 200, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...extra },
  });
}

function errorResponse(status, code, message) {
  return json({ error: { code, message } }, status);
}

function publicConfig(cfg) {
  const { private: _hidden, ...pub } = cfg;
  return pub;
}

function timingSafeEqual(a, b) {
  const enc = new TextEncoder();
  const x = enc.encode(a);
  const y = enc.encode(b);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}

function requireAdmin(s, request) {
  if (!s.adminKey) throw new HttpError(404, "not_found", "Funciones privadas desactivadas (falta ADMIN_API_KEY)");
  const key = request.headers.get("X-Admin-Key") || "";
  if (!key || !timingSafeEqual(key, s.adminKey)) throw new HttpError(401, "unauthorized", "X-Admin-Key ausente o incorrecta");
}

function appVersionParam(url) {
  const v = url.searchParams.get("app_version");
  if (v === null) return null;
  try {
    parseVersion(v);
  } catch (e) {
    throw new HttpError(422, "invalid_request", `app_version: ${e.message}`);
  }
  return v;
}

// Plan del cliente. Hoy FPSMatrix es 100 % gratis -> siempre "free".
// Para premium: verificar acá la licencia firmada (Ed25519) que mande la app.
// Nunca confiar en un plan que la app diga sin firma.
function clientTier(_request) {
  return "free";
}

function effectiveFeatures(cfg, appVersion, tier) {
  const out = {};
  for (const [name, f] of Object.entries(cfg.features)) {
    let enabled = f.enabled;
    let reason = null;
    if (!f.enabled) reason = "apagada en la configuración";
    else if (cfg.kill_switch.enabled) (enabled = false), (reason = "kill switch activo");
    else if (f.tier === "premium" && tier !== "premium") (enabled = false), (reason = "requiere plan premium");
    else if (f.min_app_version && (!appVersion || compareVersions(appVersion, f.min_app_version) < 0))
      (enabled = false), (reason = `requiere FPSMatrix ${f.min_app_version} o superior`);
    else if (f.sensitive) reason = "necesita que el usuario lo autorice en la app";
    out[name] = { enabled, sensitive: f.sensitive, tier: f.tier, reason };
  }
  return out;
}

async function sha256Hex(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ---------- router ----------

async function handle(request, env, ctx) {
  const s = settings(env);
  const url = new URL(request.url);
  const route = `${request.method} ${url.pathname.replace(/\/+$/, "") || "/"}`;
  let state = await loadState(s);

  // Si el cron no corrió hace rato (o no está configurado), refrescar en segundo plano.
  const last = Date.parse(state.meta.last_fetch_attempt_at || 0) || 0;
  if (s.repo && Date.now() - last > s.interval * 2000 && route !== "POST /admin/refresh")
    ctx.waitUntil(refresh(s, state).catch(() => {}));

  switch (route) {
    case "GET /":
    case "GET /health": {
      const m = state.meta;
      return json({
        status: s.repo && m.last_error ? "degraded" : "ok",
        api_version: API_VERSION,
        config_source: state.source,
        config_version: state.config.config_version,
        last_fetch_ok_at: m.last_fetch_ok_at || null,
        last_fetch_attempt_at: m.last_fetch_attempt_at || null,
        last_error: m.last_error || null,
        github_repo_configured: Boolean(s.repo),
        offline_copy_enabled: Boolean(s.kv),
      });
    }
    case "GET /v1/config": {
      const body = JSON.stringify(publicConfig(state.config));
      const etag = `"${(await sha256Hex(body)).slice(0, 32)}"`;
      const headers = { ETag: etag, "Cache-Control": "public, max-age=60", "X-Config-Source": state.source };
      if (request.headers.get("If-None-Match") === etag) return new Response(null, { status: 304, headers });
      return new Response(body, { headers: { "Content-Type": "application/json; charset=utf-8", ...headers } });
    }
    case "GET /v1/version": {
      const v = appVersionParam(url);
      const { app, compatibility, config_version } = state.config;
      const out = {
        latest_version: app.latest_version,
        min_supported_version: app.min_supported_version,
        config_version,
        download_url: app.download_url,
      };
      if (v) {
        const blocked = compatibility.blocked_app_versions.includes(v);
        Object.assign(out, {
          app_version: v,
          update_available: compareVersions(v, app.latest_version) < 0,
          supported: compareVersions(v, app.min_supported_version) >= 0 && !blocked,
          blocked,
        });
      }
      return json(out);
    }
    case "GET /v1/features": {
      const v = appVersionParam(url);
      const tier = clientTier(request);
      return json({
        app_version: v,
        tier,
        config_version: state.config.config_version,
        features: effectiveFeatures(state.config, v, tier),
      });
    }
    case "GET /admin/config":
      requireAdmin(s, request);
      return json(state.config);
    case "POST /admin/refresh": {
      requireAdmin(s, request);
      try {
        const r = await refresh(s, state);
        return json({ updated: r.updated, config_version: r.state.config.config_version, config_source: r.state.source, detail: r.detail });
      } catch (e) {
        if (e instanceof HttpError)
          throw new HttpError(e.status, e.code, `${e.message}. Se conserva la config v${state.config.config_version}`);
        throw e;
      }
    }
    default:
      if (["/health", "/v1/config", "/v1/version", "/v1/features", "/admin/config", "/admin/refresh"].includes(url.pathname))
        throw new HttpError(405, "method_not_allowed", "Método no permitido");
      throw new HttpError(404, "not_found", "Ruta inexistente");
  }
}

export default {
  async fetch(request, env, ctx) {
    try {
      return await handle(request, env, ctx);
    } catch (e) {
      if (e instanceof HttpError) return errorResponse(e.status, e.code, e.message);
      console.error("error no controlado:", e);
      return errorResponse(500, "internal_error", "Error interno. La configuración anterior sigue activa.");
    }
  },

  // Cron Trigger (wrangler.toml): busca cambios en GitHub cada 5 minutos.
  async scheduled(_event, env, ctx) {
    const s = settings(env);
    if (!s.repo) return;
    ctx.waitUntil(
      loadState(s)
        .then((state) => refresh(s, state))
        .then((r) => console.log(r.detail))
        .catch((e) => console.warn("actualización fallida, se conserva la última válida:", e.message)),
    );
  },
};
