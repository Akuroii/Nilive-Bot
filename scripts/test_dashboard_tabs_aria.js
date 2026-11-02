// A panel previously marked aria-hidden must become accessible when selected.
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const listeners = {};
function element(id) {
  return {id, dataset: {}, attributes: {}, style: {}, hidden: false,
    setAttribute(k,v) { this.attributes[k] = v; },
    getAttribute(k) { return this.attributes[k] || null; },
    hasAttribute(k) { return Object.hasOwn(this.attributes,k); },
    addEventListener() {}, focus() {}};
}
const panels = ['logs', 'warnings'].map(n => element('tab-content-' + n));
panels[1].style.display = 'none';
const group = element('group');
const tabs = ['logs', 'warnings'].map(n => {
  const e = element('tab-' + n); e.dataset.tab=n; e.parentElement=group;
  e.closest=()=>e;
  e.click=()=>{
    panels.forEach((p,i)=>{p.style.display=i===tabs.indexOf(e)?'block':'none';});
    listeners.click({target:e});
  };
  return e;
});
group.querySelectorAll=()=>tabs;
const all = [...tabs,...panels];
const document = {querySelectorAll:()=>tabs, querySelector:()=>null,
  getElementById:id=>all.find(e=>e.id===id) || null,
  addEventListener:(name,fn)=>{listeners[name]=fn;}};
const window = {location:{href:'https://example.test/moderation',pathname:'/moderation',hash:''},
  getComputedStyle:el=>({display:el.style.display || 'block'}),
  history:{pushState(){},replaceState(){}}, addEventListener(){}, setTimeout,
};
const context = {window, document, URL, sessionStorage:{getItem(){return null},setItem(){}}, Set};
vm.runInNewContext(fs.readFileSync('dashboard/static/js/dashboard-tabs.js','utf8'), context);
assert.equal(panels[1].getAttribute('aria-hidden'),'true');
tabs[1].click();
assert.equal(panels[1].getAttribute('aria-hidden'),'false');
assert.equal(panels[0].getAttribute('aria-hidden'),'true');
console.log('Dashboard tab ARIA visibility passed');
