// Validación de config.json (mismas reglas que models.py de la versión Python).
//
// Todo lo que llega de GitHub pasa por acá antes de usarse:
//  * claves desconocidas -> se rechaza el archivo entero
//  * parámetros: solo bool, número o texto corto (nunca código)
//  * URLs: solo https y de un dominio permitido
//  * funciones "sensitive": la app debe pedir permiso al usuario

export const SUPPORTED_SCHEMA_VERSION = 1;

const VERSION_RE = /^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.]+))?$/;
const KEY_RE = /^[a-z0-9][a-z0-9_.\-]{0,63}$/;
const LANG_RE = /^[a-z]{2}$/;
const MAX_TEXT = 500;

export class ConfigRejected extends Error {}

export function parseVersion(v) {
  const m = typeof v === "string" && v.length <= 32 ? VERSION_RE.exec(v.trim()) : null;
  if (!m) throw new Error(`versión inválida: ${JSON.stringify(v)} (se espera X.Y.Z o X.Y.Z-sufijo)`);
  return [Number(m[1]), Number(m[2]), Number(m[3]), m[4] ? 0 : 1, m[4] || ""];
}

// -1 si a < b, 0 si iguales, 1 si a > b. 1.9.5-u < 1.9.5 < 1.9.6
export function compareVersions(a, b) {
  const pa = parseVersion(a);
  const pb = parseVersion(b);
  for (let i = 0; i < pa.length; i++) {
    if (pa[i] < pb[i]) return -1;
    if (pa[i] > pb[i]) return 1;
  }
  return 0;
}

class Checker {
  constructor(allowedHosts) {
    this.allowedHosts = allowedHosts;
    this.errors = [];
  }
  fail(path, msg) {
    this.errors.push(`${path || "(raíz)"}: ${msg}`);
  }
  isObj(v) {
    return v !== null && typeof v === "object" && !Array.isArray(v);
  }
  obj(v, path, allowed, required = []) {
    if (!this.isObj(v)) return this.fail(path, "debe ser un objeto"), false;
    for (const k of Object.keys(v)) if (!allowed.includes(k)) this.fail(`${path}.${k}`, "campo no permitido");
    for (const k of required) if (!(k in v)) this.fail(`${path}.${k}`, "campo obligatorio");
    return true;
  }
  bool(v, path) {
    if (typeof v !== "boolean") this.fail(path, "debe ser true o false");
  }
  int(v, path, min = 0) {
    if (!Number.isInteger(v) || v < min) this.fail(path, `debe ser un entero >= ${min}`);
  }
  text(v, path, max = MAX_TEXT) {
    if (typeof v !== "string" || v.length > max) this.fail(path, `debe ser texto de hasta ${max} caracteres`);
  }
  version(v, path) {
    try {
      parseVersion(v);
    } catch (e) {
      this.fail(path, e.message);
    }
  }
  url(v, path) {
    if (typeof v !== "string" || v.length > 300) return this.fail(path, "URL inválida");
    let u;
    try {
      u = new URL(v);
    } catch {
      return this.fail(path, "URL inválida");
    }
    if (u.protocol !== "https:") return this.fail(path, `la URL debe usar https: ${v}`);
    if (this.allowedHosts.length && !this.allowedHosts.includes(u.hostname.toLowerCase()))
      this.fail(path, `dominio no permitido en la URL: ${u.hostname}`);
  }
  langMap(v, path) {
    if (!this.isObj(v)) return this.fail(path, "debe ser un objeto {idioma: texto}");
    for (const [k, t] of Object.entries(v)) {
      if (!LANG_RE.test(k)) this.fail(`${path}.${k}`, "código de idioma inválido (usar es, en, pt, ru...)");
      this.text(t, `${path}.${k}`);
    }
  }
  keyedMap(v, path, max, what, each) {
    if (!this.isObj(v)) return this.fail(path, "debe ser un objeto");
    const entries = Object.entries(v);
    if (entries.length > max) this.fail(path, `demasiados elementos (máx. ${max})`);
    for (const [k, val] of entries) {
      if (!KEY_RE.test(k)) this.fail(`${path}.${k}`, `nombre de ${what} inválido`);
      each(val, `${path}.${k}`);
    }
  }
  paramValue(v, path) {
    const ok =
      typeof v === "boolean" ||
      (typeof v === "number" && Number.isFinite(v)) ||
      (typeof v === "string" && v.length <= MAX_TEXT);
    if (!ok) this.fail(path, "solo se permite true/false, un número o texto corto");
  }
}

const TOP = ["schema_version", "config_version", "updated_at", "app", "compatibility", "kill_switch",
  "features", "parameters", "banners", "private"];

export function validateConfig(data, allowedHosts = []) {
  const c = new Checker(allowedHosts);
  if (!c.obj(data, "", TOP, ["schema_version", "config_version", "updated_at", "app"])) throw reject(c);

  if (data.schema_version !== SUPPORTED_SCHEMA_VERSION)
    c.fail("schema_version", `no soportado (esta API entiende ${SUPPORTED_SCHEMA_VERSION})`);
  c.int(data.config_version, "config_version", 1);
  if (typeof data.updated_at !== "string" || Number.isNaN(Date.parse(data.updated_at)))
    c.fail("updated_at", "debe ser una fecha ISO 8601");

  const app = data.app;
  if (c.obj(app, "app", ["latest_version", "min_supported_version", "download_url", "release_notes"],
    ["latest_version", "min_supported_version"])) {
    c.version(app.latest_version, "app.latest_version");
    c.version(app.min_supported_version, "app.min_supported_version");
    if (app.download_url != null) c.url(app.download_url, "app.download_url");
    if (app.release_notes != null) c.langMap(app.release_notes, "app.release_notes");
    try {
      if (compareVersions(app.min_supported_version, app.latest_version) > 0)
        c.fail("app", "min_supported_version no puede ser mayor que latest_version");
    } catch { /* ya informado */ }
  }

  if (data.compatibility != null &&
    c.obj(data.compatibility, "compatibility", ["os", "min_windows_build", "blocked_app_versions"])) {
    const cp = data.compatibility;
    if (cp.os != null) {
      if (!Array.isArray(cp.os)) c.fail("compatibility.os", "debe ser una lista");
      else cp.os.forEach((o, i) => c.text(o, `compatibility.os.${i}`, 32));
    }
    if (cp.min_windows_build != null) c.int(cp.min_windows_build, "compatibility.min_windows_build");
    if (cp.blocked_app_versions != null) {
      if (!Array.isArray(cp.blocked_app_versions)) c.fail("compatibility.blocked_app_versions", "debe ser una lista");
      else cp.blocked_app_versions.forEach((v, i) => c.version(v, `compatibility.blocked_app_versions.${i}`));
    }
  }

  if (data.kill_switch != null && c.obj(data.kill_switch, "kill_switch", ["enabled", "message"])) {
    if (data.kill_switch.enabled != null) c.bool(data.kill_switch.enabled, "kill_switch.enabled");
    if (data.kill_switch.message != null) c.langMap(data.kill_switch.message, "kill_switch.message");
  }

  if (data.features != null)
    c.keyedMap(data.features, "features", 200, "función", (f, p) => {
      if (!c.obj(f, p, ["enabled", "tier", "min_app_version", "sensitive", "description"])) return;
      if (f.enabled != null) c.bool(f.enabled, `${p}.enabled`);
      if (f.sensitive != null) c.bool(f.sensitive, `${p}.sensitive`);
      if (f.tier != null && !["free", "premium"].includes(f.tier)) c.fail(`${p}.tier`, "debe ser free o premium");
      if (f.min_app_version != null) c.version(f.min_app_version, `${p}.min_app_version`);
      if (f.description != null) c.text(f.description, `${p}.description`);
    });

  for (const sec of ["parameters", "private"])
    if (data[sec] != null) c.keyedMap(data[sec], sec, 500, "parámetro", (v, p) => c.paramValue(v, p));

  if (data.banners != null) {
    if (!Array.isArray(data.banners) || data.banners.length > 20) c.fail("banners", "debe ser una lista de hasta 20");
    else data.banners.forEach((b, i) => {
      const p = `banners.${i}`;
      if (!c.obj(b, p, ["id", "level", "text", "url", "min_app_version", "max_app_version"], ["id", "text"])) return;
      if (typeof b.id !== "string" || !KEY_RE.test(b.id)) c.fail(`${p}.id`, "id inválido");
      if (b.level != null && !["info", "warning"].includes(b.level)) c.fail(`${p}.level`, "debe ser info o warning");
      c.langMap(b.text, `${p}.text`);
      if (b.url != null) c.url(b.url, `${p}.url`);
      if (b.min_app_version != null) c.version(b.min_app_version, `${p}.min_app_version`);
      if (b.max_app_version != null) c.version(b.max_app_version, `${p}.max_app_version`);
    });
  }

  if (c.errors.length) throw reject(c);
  return normalize(data);
}

function reject(c) {
  return new ConfigRejected(`config.json inválido: ${c.errors.slice(0, 5).join("; ")}`);
}

// Completa los valores por defecto (igual que Pydantic) para respuestas estables.
function normalize(d) {
  const features = {};
  for (const [k, f] of Object.entries(d.features || {}))
    features[k] = {
      enabled: f.enabled ?? false,
      tier: f.tier ?? "free",
      min_app_version: f.min_app_version ?? null,
      sensitive: f.sensitive ?? false,
      description: f.description ?? "",
    };
  return {
    schema_version: d.schema_version,
    config_version: d.config_version,
    updated_at: d.updated_at,
    app: {
      latest_version: d.app.latest_version,
      min_supported_version: d.app.min_supported_version,
      download_url: d.app.download_url ?? null,
      release_notes: d.app.release_notes ?? {},
    },
    compatibility: {
      os: d.compatibility?.os ?? [],
      min_windows_build: d.compatibility?.min_windows_build ?? null,
      blocked_app_versions: d.compatibility?.blocked_app_versions ?? [],
    },
    kill_switch: { enabled: d.kill_switch?.enabled ?? false, message: d.kill_switch?.message ?? {} },
    features,
    parameters: d.parameters ?? {},
    banners: (d.banners ?? []).map((b) => ({
      id: b.id, level: b.level ?? "info", text: b.text, url: b.url ?? null,
      min_app_version: b.min_app_version ?? null, max_app_version: b.max_app_version ?? null,
    })),
    private: d.private ?? {},
  };
}
