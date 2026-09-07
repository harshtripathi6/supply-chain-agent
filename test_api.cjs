// Exercise the actual page helper without a browser or external requests.
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { runInNewContext } = require('node:vm');
const { test } = require('node:test');

const html = readFileSync(`${__dirname}/index.html`, 'utf8');
const helper = html.slice(html.indexOf('async function api('), html.indexOf('function setBusy('));
const apiFor = response => runInNewContext(`${helper}; api`, { fetch: async () => response });

test('plain-text server failure reports HTTP status instead of a JSON parse error', async () => {
  await assert.rejects(apiFor(new Response('Internal Server Error', { status: 500 }))('/api/runs'),
    /Request failed \(HTTP 500\).*Check the server log/);
});

test('structured API errors retain their explanation', async () => {
  await assert.rejects(apiFor(Response.json({ detail: 'Missing configuration: GEMINI_API_KEY' },
    { status: 503 }))('/api/runs'), /Missing configuration: GEMINI_API_KEY/);
});

test('valid JSON responses still return data', async () => {
  const result = await apiFor(Response.json({ id: 'run-id' }))('/api/runs');
  assert.equal(result.id, 'run-id');
});
