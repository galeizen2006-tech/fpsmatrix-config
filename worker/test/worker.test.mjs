// Pruebas sin Internet ni Cloudflare: GitHub y KV se simulan.
// Ejecutar:  npm test   (o  node --test test/worker.test.mjs)

import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";

import worker from "../src/index.js";
import DEFAULT_CONFIG from "../src/default-config.js";
import { compareVersions } from "../src/validate.js";

const ADMIN = { "X-Admin-Key": "clave-de-prueba" };
const BASE_VERSION = DEFAULT_CONFIG.config_version;

class FakeKV {
  constructor() { this.data = new Map(); }
  async get(k) { return this.data.has(k) ? this.data.get(k) : null; }
  async put(k, v) { this.data.set(k, v); }
}

let gh; // lo que "responde GitHub"
let requests;

beforeEach(() => {
  gh = { status: 200, payload: structuredClone(DEFAULT_CONFIG), raw: null, fail: false };
  requests = [];
  globalThis.fetch = async (url, init = {}) => {
    requests.push({ url: String(url), headers: init.headers || {} });
    if (gh.fail) throw new TypeError("fetch failed");
    const body = gh.raw ?? JSON.stringify(gh.payload);
    return new Response(gh.status === 200 ? body : "x", { status: gh.status, headers: { ETag: '"abc"' } });
  };
});

function makeEnv(over = {}) {
  return {
    GITHUB_REPO: "usuario/fpsmatrix-config",
    ADMIN_API_KEY: "clave-de-prueba",
    ALLOWED_URL_HOSTS: "github.com,fpsmatrix.com",
    CONFIG_KV: new FakeKV(),
    ...over,
  };
}

const ctx = { waitUntil() {} };

async function call(env, path, { method = "GET", headers = {} } = {}) {
  const res = await worker.fetch(new Request(`https://config.fpsmatrix.com${path}`, { method, headers }), env, ctx);
  const text = await res.text();
  return { status: res.status, headers: res.headers, text, body: text ? JSON.parse(text) : null };
}

const refreshNow = (env) => call(env, "/admin/refresh", { method: "POST", headers: ADMIN });

function newer(v, extra = {}) {
  gh.payload.config_version = v;
  Object.assign(gh.payload, extra);
}

test("health y configuración de fábrica", async () => {
  const r = await call(makeEnv(), "/health");
  assert.equal(r.status, 200);
  assert.equal(r.body.status, "ok");
  assert.equal(r.body.config_source, "bundled");
  assert.equal(r.body.config_version, BASE_VERSION);
});

test("la config pública no muestra 'private' y usa ETag", async () => {
  const env = makeEnv();
  const r = await call(env, "/v1/config");
  assert.equal(r.status, 200);
  assert.ok(!("private" in r.body));
  assert.equal(r.body.parameters["fps.competitive_cap_hz"], 360);
  const r2 = await call(env, "/v1/config", { headers: { "If-None-Match": r.headers.get("ETag") } });
  assert.equal(r2.status, 304);
});

test("versiones", async () => {
  const env = makeEnv();
  let r = await call(env, "/v1/version?app_version=1.9.4");
  assert.equal(r.body.update_available, true);
  assert.equal(r.body.supported, true);
  r = await call(env, "/v1/version?app_version=1.8.0");
  assert.equal(r.body.supported, false);
  r = await call(env, "/v1/version?app_version=uno");
  assert.equal(r.status, 422);
  assert.equal(r.body.error.code, "invalid_request");
  assert.equal(compareVersions("1.9.5-u", "1.9.5"), -1);
  assert.equal(compareVersions("1.10.0", "1.9.9"), 1);
});

test("funciones según versión, premium y sensibles", async () => {
  const env = makeEnv();
  let f = (await call(env, "/v1/features?app_version=1.9.4")).body.features;
  assert.equal(f.benchmark.enabled, true);
  assert.equal(f.game_mode_priority.enabled, false);
  assert.equal(f.advanced_reports.enabled, false);
  f = (await call(env, "/v1/features?app_version=1.9.5")).body.features;
  assert.equal(f.game_mode_priority.enabled, true);
  assert.equal(f.game_mode_priority.sensitive, true);
});

test("admin pide clave, y sin ADMIN_API_KEY no existe", async () => {
  const env = makeEnv();
  assert.equal((await call(env, "/admin/config")).status, 401);
  assert.equal((await call(env, "/admin/config", { headers: { "X-Admin-Key": "mala" } })).status, 401);
  const ok = await call(env, "/admin/config", { headers: ADMIN });
  assert.equal(ok.status, 200);
  assert.ok("private" in ok.body);
  assert.equal((await call(makeEnv({ ADMIN_API_KEY: "" }), "/admin/config", { headers: ADMIN })).status, 404);
});

test("refresh aplica la versión nueva y la guarda en KV", async () => {
  const env = makeEnv();
  newer(BASE_VERSION + 1);
  gh.payload.parameters["fps.competitive_cap_hz"] = 240;
  const r = await refreshNow(env);
  assert.equal(r.status, 200);
  assert.equal(r.body.updated, true);
  assert.equal((await call(env, "/v1/config")).body.parameters["fps.competitive_cap_hz"], 240);
  assert.ok(env.CONFIG_KV.data.has("config"));
  assert.match(requests[0].url, /^https:\/\/raw\.githubusercontent\.com\/usuario\/fpsmatrix-config\/main\/config\.json$/);
  assert.equal(requests[0].headers.Authorization, undefined);
});

test("sin Internet sigue sirviendo la copia de KV", async () => {
  const env = makeEnv();
  newer(BASE_VERSION + 2);
  await refreshNow(env);
  gh.fail = true;
  const r = await refreshNow(env);
  assert.equal(r.status, 502);
  assert.equal(r.body.error.code, "github_unreachable");
  const h = (await call(env, "/health")).body;
  assert.equal(h.status, "degraded");
  assert.equal(h.config_version, BASE_VERSION + 2);
});

const BAD = [
  ["campo extra con script", (p) => (p.features = { x: { enabled: true, run_script: "del C:\\" } })],
  ["comando en parámetros", (p) => (p.parameters.cmd = ["powershell", "-c", "..."])],
  ["URL http", (p) => (p.app.download_url = "http://github.com/x")],
  ["dominio no permitido", (p) => (p.app.download_url = "https://evil.example.com/x.exe")],
  ["mínima > última", (p) => (p.app.min_supported_version = "9.9.9")],
  ["schema desconocido", (p) => (p.schema_version = 99)],
];
for (const [name, mutate] of BAD) {
  test(`rechaza config inválida: ${name}`, async () => {
    const env = makeEnv();
    newer(BASE_VERSION + 5);
    mutate(gh.payload);
    const r = await refreshNow(env);
    assert.equal(r.status, 422, r.text);
    assert.equal(r.body.error.code, "config_rejected");
    assert.equal((await call(env, "/v1/config")).body.config_version, BASE_VERSION);
  });
}

test("JSON roto se rechaza", async () => {
  const env = makeEnv();
  gh.raw = "{ esto no es json";
  const r = await refreshNow(env);
  assert.equal(r.status, 502);
  assert.equal(r.body.error.code, "github_bad_content");
});

test("no deja volver a una versión más vieja", async () => {
  const env = makeEnv();
  newer(BASE_VERSION + 3);
  await refreshNow(env);
  newer(BASE_VERSION + 1);
  assert.equal((await refreshNow(env)).status, 422);
  assert.equal((await call(env, "/v1/config")).body.config_version, BASE_VERSION + 3);
});

test("kill switch apaga todas las funciones", async () => {
  const env = makeEnv();
  newer(BASE_VERSION + 1, { kill_switch: { enabled: true, message: { es: "Mantenimiento" } } });
  await refreshNow(env);
  const f = (await call(env, "/v1/features?app_version=1.9.5")).body.features;
  assert.ok(Object.values(f).every((x) => !x.enabled));
});

test("repo privado: el token va solo a GitHub y nunca sale en respuestas", async () => {
  const env = makeEnv({ FPSMATRIX_GITHUB_TOKEN: "token-falso-de-prueba" });
  newer(BASE_VERSION + 1);
  await refreshNow(env);
  assert.match(requests[0].url, /^https:\/\/api\.github\.com\//);
  assert.equal(requests[0].headers.Authorization, "Bearer token-falso-de-prueba");
  for (const p of ["/health", "/v1/config", "/v1/version"]) assert.ok(!(await call(env, p)).text.includes("token-falso"));
  assert.ok(!(await call(env, "/admin/config", { headers: ADMIN })).text.includes("token-falso"));
});

test("sin GITHUB_REPO funciona con la config de fábrica", async () => {
  const env = makeEnv({ GITHUB_REPO: "" });
  const h = (await call(env, "/health")).body;
  assert.equal(h.status, "ok");
  assert.equal(h.github_repo_configured, false);
  const r = await refreshNow(env);
  assert.equal(r.status, 503);
  assert.equal(r.body.error.code, "github_not_configured");
});

test("rutas y métodos inexistentes", async () => {
  const env = makeEnv();
  assert.equal((await call(env, "/nada")).status, 404);
  assert.equal((await call(env, "/v1/config", { method: "POST" })).status, 405);
});
