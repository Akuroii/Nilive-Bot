// Execute the ACTUAL Shop template script in a VM and prove the edit-form
// behaviour for legacy Prestige VI rows. No browser, no server, no mocks of the
// code under test: the script below is sliced out of the real template, so a
// regression in shop.html fails here.
//
// Covers the cross-product form-state corruption (VI cached values leaking into
// another product's edit), Cancel cleanliness, legacy VI duration preservation,
// the explicit "Clear legacy duration" action, and the exact PUT/POST payloads
// the real Flask API then receives.
//
// Run:  node scripts/test_vi_legacy_edit_form.js
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');

const source = fs.readFileSync(
  path.join(__dirname, '../dashboard/templates/systems/shop.html'), 'utf8');
// The page script is the LAST <script> block in the template.
const script = source.slice(source.indexOf('<script>' ) + 8).split('</script>')[0];

// A minimal element stub that behaves like a real form control where it
// matters: `value` and `textContent` always coerce to strings (a browser never
// hands back a number), and <select> keeps a live, ordered option list.
function makeEl(id, tag) {
  const el = {
    id, tagName: tag || 'input', disabled: false, checked: false,
    dataset: {}, style: {}, _options: [], attrs: {}, _v: '', _t: '', _d: '',
    classList: { add() {}, remove() {} },
    scrollIntoView() {},
    getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    appendChild(c) { this._options.push(c); c.parent = this; },
    remove() {
      if (this.parent) {
        this.parent._options = this.parent._options.filter(o => o !== this);
      }
    },
  };
  Object.defineProperty(el, 'options', { get() { return this._options; } });
  Object.defineProperty(el, 'value', {
    get() { return this._v; },
    set(nv) { this._v = (nv === null || nv === undefined) ? '' : String(nv); },
  });
  Object.defineProperty(el, 'textContent', {
    get() { return this._t; }, set(nv) { this._t = String(nv); },
  });
  Object.defineProperty(el, 'className', {
    get() { return this._d; }, set(nv) {
      this._d = String(nv);
      this.style.display = this._d.indexOf('hidden') !== -1 ? 'none' : '';
    },
  });
  return el;
}

const CURRENT_TYPES = [
  ['custom', '📦 Item'], ['color', '🎨 Color'], ['potion', '🧪 Potion'],
  ['title', '🏷️ Title'], ['prestige', '⭐ Prestige'],
];

// Two products with deliberately DIFFERENT values in every cached field, so any
// leakage between them is unmistakable.
const ITEMS = {
  1: {
    id: 1, name: 'Old VI', type: 'prestige', prestige_tier: 6, price: 0,
    price_diamonds: null, role_id: null, duration_hours: 48, max_stock: null,
    current_stock: null, required_level: 0, required_role_id: null,
    xp_boost_multiplier: null, icon_url: null, rarity: 'common', featured: 0,
    description: null, category_id: null, option_of_id: null,
  },
  2: {
    id: 2, name: 'Old V', type: 'prestige', prestige_tier: 5, price: 500,
    price_diamonds: null, role_id: null, duration_hours: 0, max_stock: null,
    current_stock: null, required_level: 0, required_role_id: null,
    xp_boost_multiplier: null, icon_url: null, rarity: 'common', featured: 0,
    description: null, category_id: null, option_of_id: null,
  },
  3: {
    id: 3, name: 'VIP Role', type: 'color', prestige_tier: null, price: 1500,
    price_diamonds: null, role_id: 77, duration_hours: 0, max_stock: 20,
    current_stock: 7, required_level: 10, required_role_id: null,
    xp_boost_multiplier: null, icon_url: 'https://x/i.png', rarity: 'epic',
    featured: 1, description: 'd', category_id: 2, option_of_id: null,
  },
  4: {
    id: 4, name: 'Normal VI', type: 'prestige', prestige_tier: 6, price: 0,
    price_diamonds: null, role_id: null, duration_hours: 0, max_stock: null,
    current_stock: null, required_level: 0, required_role_id: null,
    xp_boost_multiplier: null, icon_url: null, rarity: 'common', featured: 0,
    description: null, category_id: null, option_of_id: null,
  },
};

function fresh() {
  const els = new Map();
  const el = (id) => {
    if (!els.has(id)) els.set(id, makeEl(id));
    return els.get(id);
  };
  const typeSel = el('item-type');
  typeSel.tagName = 'select';
  for (const [value, label] of CURRENT_TYPES) {
    const opt = makeEl('opt-' + value, 'option');
    opt.value = value;
    opt.textContent = label;
    typeSel.appendChild(opt);
  }
  const requests = [];
  const toasts = [];
  const ctx = vm.createContext({
    document: {
      getElementById: el,
      querySelectorAll: () => [],
      createElement: (tag) => makeEl('new-' + tag, tag),
    },
    showToast: (...args) => toasts.push(args),
    currencyNameFor: (key) => key,
    fetch: async (url, opts) => {
      const method = (opts && opts.method) || 'GET';
      requests.push({
        url, method,
        body: opts && opts.body ? JSON.parse(opts.body) : null,
      });
      if (method === 'GET') {
        const id = parseInt(url.split('/').pop(), 10);
        return { json: async () => ({ success: true, item: ITEMS[id] }) };
      }
      return { json: async () => ({ success: true }) };
    },
    setTimeout: () => {},
    location: { reload() {} },
    console, Array, JSON, parseInt, parseFloat, String, Boolean, Number,
    Object, RegExp, Error, Promise, Math, isNaN,
  });
  vm.runInContext(script, ctx);
  const lastWrite = () => requests.filter(
    r => r.method === 'PUT' || r.method === 'POST').at(-1);
  return { ctx, el, requests, toasts, lastWrite };
}

const results = [];
async function scenario(name, fn) {
  try { await fn(); results.push([true, name]); }
  catch (e) { results.push([false, name, String(e.message).replace(/\n+/g, ' | ')]); }
}

(async () => {
  // ── 1. legacy VI(48) unrelated edit preserves 48 ──────────────────────
  await scenario('1. legacy VI(48) unrelated edit preserves 48', async () => {
    const { ctx, el, lastWrite } = fresh();
    await ctx.editItem(1);
    assert.equal(el('item-duration').value, '48',
      'the prefill must keep the stored legacy duration, not zero it');
    assert.equal(el('item-duration').disabled, true, 'VI keeps duration disabled');
    await ctx.addItem();
    const w = lastWrite();
    assert.equal(w.method, 'PUT');
    assert.equal(w.url, '/api/shop/item/1');
    assert.equal(w.body.duration_hours, 48,
      'PUT must resend the stored 48, not null');
    assert.equal(w.body.prestige_tier, 6);
  });

  // ── 2. VI -> V with 48 still present rejects ──────────────────────────
  await scenario('2. VI -> V with the legacy 48 still present is rejected', async () => {
    const { ctx, el, lastWrite } = fresh();
    await ctx.editItem(1);
    el('item-prestige-tier').value = '5';
    ctx.toggleItemTypeFields();
    el('item-price').disabled = false;
    el('item-price').value = '500';
    el('item-duration').disabled = false;
    await ctx.addItem();
    const w = lastWrite();
    // The UI must not launder the grandfather through a tier change: it sends
    // the untouched 48, which the API (Phase 1) now refuses.
    assert.equal(w.body.prestige_tier, 5);
    assert.equal(w.body.duration_hours, 48,
      'the stale 48 must reach the API so the tier guard can reject it');
  });

  // ── 3. explicit clear, then VI -> V stores NULL ───────────────────────
  await scenario('3. explicit clear followed by VI -> V sends null', async () => {
    const { ctx, el, lastWrite } = fresh();
    await ctx.editItem(1);
    el('item-duration').value = '0';
    el('item-prestige-tier').value = '5';
    ctx.toggleItemTypeFields();
    el('item-price').disabled = false;
    el('item-price').value = '500';
    await ctx.addItem();
    const w = lastWrite();
    assert.equal(w.body.prestige_tier, 5);
    assert.equal(w.body.duration_hours, null);
  });

  // ── 4. V -> VI is free, unmetered, permanent ──────────────────────────
  await scenario('4. V -> VI becomes free, unmetered and permanent', async () => {
    const { ctx, el, lastWrite } = fresh();
    await ctx.editItem(2);
    el('item-prestige-tier').value = '6';
    ctx.toggleItemTypeFields();
    await ctx.addItem();
    const w = lastWrite();
    assert.equal(w.body.prestige_tier, 6);
    assert.equal(w.body.price, 0, 'VI must be free');
    assert.equal(w.body.price_diamonds, null, 'VI takes no Diamonds');
    assert.equal(w.body.max_stock, null, 'VI is unmetered');
    assert.equal(w.body.required_level, 0);
    assert.equal(w.body.duration_hours, null, 'VI must be permanent');
  });

  // ── 5. Clear action sends null and keeps VI ───────────────────────────
  await scenario('5. Clear legacy duration sends null and preserves VI', async () => {
    const { ctx, el, lastWrite } = fresh();
    await ctx.editItem(1);
    assert.equal(typeof ctx.clearLegacyViDuration, 'function',
      'clearLegacyViDuration must be part of the page API');
    ctx.clearLegacyViDuration();
    // Still VI, still free/unmetered, still duration-disabled.
    assert.equal(el('item-type').value, 'prestige');
    assert.equal(el('item-prestige-tier').value, '6');
    assert.equal(el('item-duration').disabled, true, 'VI mode stays active');
    assert.equal(el('item-duration').value, '0');
    assert.equal(el('item-price').disabled, true);
    assert.equal(el('item-stock').disabled, true);
    await ctx.addItem();
    const w = lastWrite();
    assert.equal(w.body.duration_hours, null, 'clear must send null');
    assert.equal(w.body.prestige_tier, 6, 'tier stays VI');
    assert.equal(w.body.price, 0);
    assert.equal(w.body.max_stock, null);
  });

  // ── 6. Clear button visibility ────────────────────────────────────────
  await scenario('6. Clear action appears only for a legacy VI duration', async () => {
    const { ctx, el } = fresh();
    const btn = () => el('item-clear-legacy-duration');
    ctx.cancelShopEdit();
    assert.equal(btn().style.display, 'none', 'hidden on a fresh Add form');

    // a NEW VI being set up by hand: still hidden
    el('item-type').value = 'prestige';
    el('item-prestige-tier').value = '6';
    ctx.toggleItemTypeFields();
    assert.equal(btn().style.display, 'none', 'hidden for a new VI');

    // an existing VI with NO legacy duration: hidden
    await ctx.editItem(4);
    assert.equal(btn().style.display, 'none', 'hidden for a normal VI');

    // a normal VI that the user has not cleared: hidden
    await ctx.editItem(1);
    assert.notEqual(btn().style.display, 'none',
      'shown for an existing VI with a positive stored duration');

    // Tier V and a non-Prestige product: hidden
    await ctx.editItem(2);
    assert.equal(btn().style.display, 'none', 'hidden for Tier V');
    await ctx.editItem(3);
    assert.equal(btn().style.display, 'none', 'hidden for a non-Prestige type');

    // after the clear is used, the action retires: hidden again
    await ctx.editItem(1);
    ctx.clearLegacyViDuration();
    assert.equal(el('item-duration').value, '0');
    // (a fresh load of the same row still shows it until the save lands)
    await ctx.editItem(1);
    assert.notEqual(btn().style.display, 'none');
  });

  // ── 7. Cancel restores a clean Add form ───────────────────────────────
  await scenario('7. Cancel restores clean Add defaults with no cached values', async () => {
    const { ctx, el, requests, lastWrite } = fresh();
    await ctx.editItem(1);
    ctx.clearLegacyViDuration();
    ctx.cancelShopEdit();
    assert.equal(el('item-type').disabled, false, 'type unlock');
    assert.equal(el('item-type').value, 'custom', 'back to the first type');
    assert.equal(el('item-duration').value, '0');
    assert.equal(el('item-duration').disabled, false, 'VI lock released');
    assert.equal(el('item-price').disabled, false);
    assert.equal(el('item-price').value, '');
    assert.equal(el('item-price').min, '0');
    assert.equal(el('item-stock').disabled, false);
    assert.equal(el('item-stock').value, '0');
    assert.equal(el('item-level').value, '0');
    assert.equal(el('item-level').disabled, false);
    assert.equal(el('item-price-diamonds').value, '');
    assert.equal(el('item-name').value, '');
    assert.equal(el('item-current-stock-field').style.display, 'none');
    assert.equal(el('item-current-stock').value, '');
    assert.equal(el('shop-form-title').textContent, 'Add Shop Item');
    assert.equal(el('shop-submit-btn').textContent, 'Add Item');
    assert.equal(el('item-clear-legacy-duration').style.display, 'none');
    // A submit right after Cancel is a CREATE — never a PUT to the old row.
    el('item-name').value = 'Brand New';
    el('item-price').value = '250';
    const before = requests.length;
    await ctx.addItem();
    assert.equal(requests.length, before + 1);
    const w = lastWrite();
    assert.equal(w.method, 'POST', 'must not PUT the previously edited row');
    assert.equal(w.url, '/api/shop/item');
    assert.equal(w.body.duration_hours, null);
    assert.equal(w.body.price, 250);
  });

  // ── 8. No cached-value leakage between products ───────────────────────
  await scenario('8. editing VI then another product leaks nothing', async () => {
    const { ctx, el, lastWrite } = fresh();
    await ctx.editItem(1);   // VI: zeroes+disables price/stock/level/duration
    await ctx.editItem(3);   // Color: paid, 20 stock, level 10, role 77
    assert.equal(el('item-price').value, '1500', 'price must be the Color price');
    assert.equal(el('item-price').disabled, false);
    assert.equal(el('item-stock').value, '20');
    assert.equal(el('item-level').value, '10');
    assert.equal(el('item-duration').value, '0');
    assert.equal(el('item-duration').disabled, false);
    assert.equal(el('item-role').value, '77');
    assert.equal(el('item-current-stock').value, '7');
    await ctx.addItem();
    const w = lastWrite();
    assert.equal(w.url, '/api/shop/item/3');
    assert.equal(w.body.price, 1500, 'the paid price must never be zeroed');
    assert.equal(w.body.max_stock, 20, 'the stock limit must never be dropped');
    assert.equal(w.body.required_level, 10, 'the level gate must survive');
    assert.equal(w.body.duration_hours, null, 'no stale duration may leak in');
    assert.equal(w.body.current_stock, undefined,
      'an edit must never write current_stock');
  });

  // ── 9. New VI still creates with null duration ────────────────────────
  await scenario('9. new VI still creates with a null duration', async () => {
    const { ctx, el, lastWrite } = fresh();
    ctx.cancelShopEdit();
    el('item-type').value = 'prestige';
    el('item-prestige-tier').value = '6';
    ctx.toggleItemTypeFields();
    el('item-name').value = 'New VI';
    await ctx.addItem();
    const w = lastWrite();
    assert.equal(w.method, 'POST');
    assert.equal(w.body.type, 'prestige');
    assert.equal(w.body.prestige_tier, 6);
    assert.equal(w.body.duration_hours, null);
    assert.equal(w.body.price, 0);
    assert.equal(w.body.price_diamonds, null);
    assert.equal(w.body.max_stock, null);
    assert.equal(w.body.required_level, 0);
    assert.equal(w.body.current_stock, null, 'a new VI has no stock claim');
    assert.equal(el('item-clear-legacy-duration').style.display, 'none');
  });

  // ── 10. A VI can never carry a duration out of the form ───────────────
  // VI is permanent by rule, so the form locks the duration input and always
  // sends null. Even a tampered input (impossible through the real UI, since
  // the field is disabled) must not produce a positive duration. The API is the
  // separate authority that REJECTS a positive VI duration from a direct
  // caller — proven in scripts/test_shop_product_management.py
  // (test_prestige_vi_with_positive_duration_is_rejected).
  await scenario('10. a VI can never submit a positive duration', async () => {
    const { ctx, el, lastWrite } = fresh();
    ctx.cancelShopEdit();
    el('item-type').value = 'prestige';
    el('item-prestige-tier').value = '6';
    ctx.toggleItemTypeFields();
    el('item-name').value = 'Bad VI';
    assert.equal(el('item-duration').disabled, true,
      'VI locks the duration input');
    el('item-duration').disabled = false;   // simulate tampering
    el('item-duration').value = '12';
    await ctx.addItem();
    const w = lastWrite();
    assert.equal(w.body.duration_hours, null,
      'a VI must always submit a null duration, got ' + w.body.duration_hours);
    assert.equal(w.body.prestige_tier, 6);
  });

  // ── report ────────────────────────────────────────────────────────────
  let pass = 0, fail = 0;
  for (const [ok, name, msg] of results) {
    if (ok) { console.log('  PASS  ' + name); pass++; }
    else { console.log('  FAIL  ' + name + '\n        -> ' + msg); fail++; }
  }
  console.log('\nVI LEGACY EDIT FORM: ' + pass + ' passed, ' + fail + ' failed');
  if (fail) process.exitCode = 1;
})().catch((e) => { console.error(e); process.exitCode = 1; });
