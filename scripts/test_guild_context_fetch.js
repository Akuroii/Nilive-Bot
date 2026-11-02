// Verify guild context is scoped to same-origin guild API requests.
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync('dashboard/static/js/dashboard.js', 'utf8');
const start = src.indexOf('(function patchFetchForCsrf()');
const end = src.indexOf('})();', start) + 5;
assert(start >= 0 && end > start);
const calls = [];
const window = { __GUILD_ID__: '123', __CSRF_TOKEN__: 'token',
  location: { href: 'https://dashboard.example/leveling', origin: 'https://dashboard.example' },
  fetch: (input, init) => { calls.push({input, init}); return Promise.resolve({status: 200}); } };
vm.runInNewContext(src.slice(start, end), { window, URL, Headers, console });
window.fetch('/api/leveling/config');
assert.strictEqual(calls[0].init.headers.get('X-Guild-ID'), '123');
window.fetch('/api/user/servers');
assert.strictEqual(calls[1].init.headers, undefined);
window.fetch('https://elsewhere.example/api/leveling/config');
assert.strictEqual(calls[2].init.headers, undefined);
window.fetch('/api/leveling/config', {method:'POST', headers:{'Content-Type':'application/json'}});
assert.strictEqual(calls[3].init.headers.get('X-Guild-ID'), '123');
assert.strictEqual(calls[3].init.headers.get('X-CSRF-Token'), 'token');
console.log('Guild context fetch tests passed');
