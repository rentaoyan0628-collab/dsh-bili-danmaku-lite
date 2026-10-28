import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import {
  MAX_ALLOW_ENTRIES,
  MAX_URL_LEN,
  formatAllowEntry,
  isAllowed,
  normalizeAllowlist,
  parseTarget,
} from '../lib/webhook.mjs';

test('parseTarget accepts plain http(s) targets and normalizes them', () => {
  assert.deepEqual(parseTarget('https://bot.example.com/hook'), {
    origin: 'https://bot.example.com',
    path: '/hook',
    url: 'https://bot.example.com/hook',
  });
  assert.equal(parseTarget('HTTPS://Bot.Example.COM:443/hook').origin, 'https://bot.example.com');
  assert.equal(parseTarget('http://127.0.0.1:8080').path, '/');
  assert.equal(parseTarget('  http://127.0.0.1:9999/wake  ').origin, 'http://127.0.0.1:9999');
});

test('parseTarget refuses anything a model could abuse to reach odd places', () => {
  const bad = [
    '',
    '   ',
    'ftp://example.com/hook',
    'file:///etc/passwd',
    'javascript:alert(1)',
    'https://user:pass@example.com/hook',
    'https://exa mple.com/hook',
    'https://example.com:99999999/hook',
    'x'.repeat(MAX_URL_LEN + 1),
  ];
  for (const raw of bad) assert.equal(parseTarget(raw), null, raw);
});

test('normalizeAllowlist dedupes, drops junk and caps the list', () => {
  const list = normalizeAllowlist([
    'https://a.example.com/hook',
    'https://a.example.com/hook',
    ' https://a.example.com:443/hook ',
    'nope',
    ...Array.from({ length: 40 }, (_, i) => `https://h${i}.example.com/hook`),
  ]);
  assert.equal(list[0].url, 'https://a.example.com/hook');
  const keys = list.map(formatAllowEntry);
  assert.equal(new Set(keys).size, keys.length);
  assert.ok(list.length <= MAX_ALLOW_ENTRIES);
  assert.ok(keys.every((item) => !item.includes('nope')));
  assert.deepEqual(normalizeAllowlist('nope'), []);
});

test('an origin-only entry allows any path on that origin; a path entry stays exact', () => {
  const originWide = ['https://bot.example.com'];
  assert.equal(isAllowed('https://bot.example.com/hook', originWide).ok, true);
  assert.equal(isAllowed('https://bot.example.com/a/b/c', originWide).ok, true);
  assert.equal(isAllowed('https://bot.example.com.evil.example/hook', originWide).ok, false);
  assert.equal(isAllowed('http://bot.example.com/hook', originWide).ok, false);

  const exact = ['https://bot.example.com/hook'];
  assert.equal(isAllowed('https://bot.example.com/hook', exact).ok, true);
  assert.equal(isAllowed('https://bot.example.com/hook2', exact).ok, false);
  assert.equal(isAllowed('https://bot.example.com/hook/deeper', exact).ok, false);
});

test('empty or unmatched allowlist fails closed with an actionable message', () => {
  const empty = isAllowed('https://bot.example.com/hook', []);
  assert.equal(empty.ok, false);
  assert.match(empty.error, /webhookAllowlist/);

  const denied = isAllowed('https://evil.example/hook', ['https://bot.example.com/hook']);
  assert.equal(denied.ok, false);
  assert.match(denied.error, /不在 webhookAllowlist/);

  assert.equal(isAllowed('not a url', ['https://bot.example.com']).ok, false);
});

test('formatAllowEntry stores origin only when the path is root', () => {
  assert.equal(formatAllowEntry({ origin: 'https://bot.example.com', path: '/' }), 'https://bot.example.com');
  assert.equal(formatAllowEntry({ origin: 'https://bot.example.com', path: '/hook' }), 'https://bot.example.com/hook');
});

test('plugin wires the gate into the tool, the web route and the settings route', () => {
  const index = fs.readFileSync(new URL('../index.js', import.meta.url), 'utf8');
  const python = fs.readFileSync(new URL('../python/live_mcp.py', import.meta.url), 'utf8');
  const worker = fs.readFileSync(new URL('../python/live_chat_worker.py', import.meta.url), 'utf8');
  assert.match(index, /webhook_not_allowed/);
  assert.match(index, /webhookAllowlist/);
  assert.match(python, /webhook_not_allowed/);
  assert.match(python, /def webhook_allowed/);
  assert.match(worker, /webhook_allowed/);
  assert.match(worker, /MAX_RESP_BYTES/);
  assert.match(worker, /_NoRedirect/);
});
