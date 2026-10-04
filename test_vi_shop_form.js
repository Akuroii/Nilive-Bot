// Execute the actual Shop template's script in a small DOM harness; no browser/server.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../dashboard/templates/systems/shop.html'), 'utf8');
const script = source.split('<script>')[1].split('</script>')[0];
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', disabled: false, checked: false, dataset: {}, style: {}, textContent: '',
    classList: { add() {}, remove() {} },
  });
  return elements.get(id);
}
element('item-type').value = 'prestige';
element('item-prestige-tier').value = '3';
element('item-name').value = 'Prestige';
element('item-price').value = '2500';
element('item-stock').value = '3';
element('item-level').value = '50';
element('item-duration').value = '0';
element('item-rarity').value = 'common';
const sent = [], toasts = [];
const context = vm.createContext({
  document: { getElementById: element, querySelectorAll: () => [] },
  showToast: (...args) => toasts.push(args), currencyNameFor: key => key,
  fetch: async (url, opts) => { sent.push({ url, data: JSON.parse(opts.body) }); return { json: async () => ({ success: true }) }; },
  setTimeout: () => {}, location: { reload() {} }, console,
});
vm.runInContext(script, context);
(async () => {
  await context.addItem();
  assert.equal(sent.at(-1).data.price, 2500);
  assert.equal(sent.at(-1).data.max_stock, 3);
  assert.equal(sent.at(-1).data.required_level, 50);
  element('item-prestige-tier').value = '6';
  context.toggleItemTypeFields();
  for (const id of ['item-price', 'item-price-diamonds', 'item-stock', 'item-level', 'item-duration']) {
    assert.equal(element(id).disabled, true, id);
    assert.equal(element(id).value, '0', id);
  }
  assert.match(element('item-duration-label').textContent, /Free/);
  assert.match(element('item-duration-hint').textContent, /expiry removes VI/);
  await context.addItem();
  assert.equal(sent.at(-1).data.price, 0);
  assert.equal(sent.at(-1).data.prestige_tier, 6);
  assert.equal(sent.at(-1).data.max_stock, null);
  assert.equal(sent.at(-1).data.current_stock, null);
  assert.equal(sent.at(-1).data.required_level, 0);
  assert.equal(sent.at(-1).data.price_diamonds, null);
  element('item-prestige-tier').value = '3';
  context.toggleItemTypeFields();
  assert.equal(element('item-price').disabled, false);
  assert.equal(element('item-price').value, '2500');
  assert.equal(element('item-stock').value, '3');
  assert.equal(element('item-level').value, '50');
  element('item-price').value = '0';
  const count = sent.length;
  await context.addItem();
  assert.equal(sent.length, count, 'paid I–V must not use the VI zero-price exception');
  element('item-type').value = 'title';
  context.toggleItemTypeFields();
  await context.addItem();
  assert.equal(sent.length, count, 'ordinary free items must not use the VI exception');
  assert.match(toasts.at(-1)[0], /price/);
  assert.equal(element('item-price').min, '0', 'diamond-only items may have zero Coin price');
  element('item-price-diamonds').value = '5';
  await context.addItem();
  assert.equal(sent.length, count + 1);
  assert.equal(sent.at(-1).data.price, 0);
  assert.equal(sent.at(-1).data.price_diamonds, 5);
  console.log('PASS: actual Shop form — paid values preserved; VI Free/unmetered; paid zero rejected');
})().catch(error => { console.error(error); process.exitCode = 1; });
