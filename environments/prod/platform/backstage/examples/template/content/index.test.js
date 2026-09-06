const test = require('node:test');
const assert = require('node:assert/strict');
const server = require('./index');
test('health, readiness, and metrics endpoints are available', async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  for (const path of ['/healthz', '/readyz', '/metrics']) assert.equal((await fetch(base + path)).status, 200);
  await new Promise(resolve => server.close(resolve));
});
