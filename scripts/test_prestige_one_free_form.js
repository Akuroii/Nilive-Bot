// Prestige I (Free) on the actual Shop form: a TYPED 0 is accepted, a blank or
// invalid price is not; II-V still need a positive price; VI keeps its lock.
// Executes the real template script in a small DOM harness (no browser/server).
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
element('item-name').value = 'Prestige';
element('item-stock').value = '0';
element('item-level').value = '0';
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

async function submit(tier, price) {
  element('item-prestige-tier').value = String(tier);
  context.toggleItemTypeFields();
  element('item-price').value = price;
  const before = sent.length;
  await context.addItem();
  return sent.length > before ? sent.at(-1).data : null;
}

(async () => {
  // Prestige I: typed 0 is Free and is sent as exactly 0.
  const free = await submit(1, '0');
  assert.ok(free, 'Prestige I with a typed 0 must be sent');
  assert.equal(free.price, 0);
  assert.equal(free.prestige_tier, 1);
  assert.equal(free.price_diamonds, null);
  assert.equal(element('item-price').min, '0', 'Prestige I price input allows 0');
  // Prestige I with a positive minimum balance is still allowed (unchanged).
  assert.equal((await submit(1, '25')).price, 25);

  // Prestige I: blank / junk / negative / decimal must NOT become Free.
  for (const bad of ['', '   ', 'free', '-5', '2.5']) {
    assert.equal(await submit(1, bad), null, `Prestige I ${JSON.stringify(bad)} must not send`);
    assert.match(toasts.at(-1)[0], /price/);
  }

  // Prestige II-V: zero / blank / junk rejected, positive accepted, min stays 1.
  for (const tier of [2, 3, 4, 5]) {
    for (const bad of ['0', '', '-1', 'x']) {
      assert.equal(await submit(tier, bad), null, `tier ${tier} ${JSON.stringify(bad)} must not send`);
      assert.match(toasts.at(-1)[0], /price/);
    }
    assert.equal((await submit(tier, '140')).price, 140);
    assert.equal(element('item-price').min, '1', `tier ${tier} price input needs >= 1`);
  }

  // Prestige VI: unchanged locked Free listing.
  const vi = await submit(6, '999');
  assert.equal(vi.price, 0);
  assert.equal(vi.prestige_tier, 6);
  assert.equal(element('item-price').disabled, true);

  // Switching back from VI to I restores an editable, typable zero.
  element('item-prestige-tier').value = '1';
  context.toggleItemTypeFields();
  assert.equal(element('item-price').disabled, false);
  assert.equal(element('item-price').min, '0');
  console.log('PASS: Shop form — Prestige I typed 0 accepted, blank rejected; II-V paid; VI locked');
})().catch(error => { console.error(error); process.exitCode = 1; });
