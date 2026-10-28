import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { tmpdir } from 'node:os';
const source = fs.readFileSync(new URL('../index.js', import.meta.url), 'utf8');
function functions(start, end, bindings) {
  const text = source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
  return vm.runInNewContext(text + '\n({ makeRouteGuard: typeof makeRouteGuard === "function" ? makeRouteGuard : undefined, pythonPathAllowed: typeof pythonPathAllowed === "function" ? pythonPathAllowed : undefined, asrPythonAllowed: typeof asrPythonAllowed === "function" ? asrPythonAllowed : undefined })', bindings);
}
const log = () => {};
const sendJson = (res, status, obj) => { res.statusCode = status; res.end(JSON.stringify(obj)); };
async function guardResult(ctx, headers = { host: '127.0.0.1:49984', origin: 'http://127.0.0.1:49984' }) {
  const { makeRouteGuard } = functions('function makeRouteGuard(ctx)', 'function registerRoutes(ctx)', { log, sendJson, URL, Reflect });
  const res = { statusCode: 200, end() { this.ended = true; } };
  const allowed = await makeRouteGuard(ctx)({ headers }, res);
  return { allowed, status: res.statusCode };
}
test('missing authentication service rejects even local same-origin requests', async () => {
  assert.equal((await guardResult({})).status, 503);
  assert.equal((await guardResult({ get() { throw Error('unavailable'); } })).allowed, false);
});
test('authentication denial, exception and unexpected results fail closed', async () => {
  for (const result of [401, 403, null, false, {}]) {
    assert.equal((await guardResult({ connection: { requestRejection: () => result } })).allowed, false);
  }
  assert.equal((await guardResult({ connection: { requestRejection() { throw Error('offline'); } } })).allowed, false);
  assert.equal((await guardResult({ connection: { requestRejection: async () => 403 } })).allowed, false);
});
test('authenticated same-origin succeeds; foreign origin or missing host fails', async () => {
  const ctx = { connection: { requestRejection: async () => undefined } };
  assert.equal((await guardResult(ctx)).allowed, true);
  assert.equal((await guardResult(ctx, { host: '127.0.0.1:49984', origin: 'http://evil.example' })).allowed, false);
  assert.equal((await guardResult(ctx, {})).allowed, false);
});
test('first-run arbitrary python.exe denied; fixed and explicit interpreters accepted', () => {
  const root = fs.mkdtempSync(path.join(tmpdir(), 'bili-live-security-'));
  try {
    const HOME = path.join(root, 'home');
    const fixed = path.join(HOME, '.venv', 'Scripts', 'python.exe');
    const arbitrary = path.join(root, 'untrusted', 'python.exe');
    for (const file of [fixed, arbitrary]) { fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, 'fixture, never executed'); }
    const cfg = { pythonPath: '', audio: { asrPython: '' } };
    const { pythonPathAllowed, asrPythonAllowed } = functions('function interpreterPathAllowed(', 'function pythonEnv()', { ...path, ...fs, HOME, cfg, process: { platform: process.platform, env: {} } });
    assert.equal(pythonPathAllowed(arbitrary).ok, false);
    assert.equal(pythonPathAllowed('python').ok, false);
    assert.equal(pythonPathAllowed(path.dirname(fixed)).ok, false);
    assert.equal(pythonPathAllowed(fixed).ok, true);
    cfg.audio.asrPython = arbitrary; // Explicit local file configuration, not a web write.
    assert.equal(pythonPathAllowed('').ok, true);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});