const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../static/js/project-infrastructure-picker.js'), 'utf8');

function picker(fetch) {
    const context = {fetch, URLSearchParams};
    vm.createContext(context);
    vm.runInContext(source, context);
    return context.projectInfrastructurePicker({type:'base_station'});
}

test('editing and clearing discard the selected identifier', () => {
    const state = picker();
    state.choose({id:'asset-1',label:'Station'});
    state.search = 'Different'; state.edited();
    assert.equal(state.id, '');
    assert.equal(state.loading, true);
    state.clear();
    assert.equal(state.type, '');
    assert.equal(state.search, '');
    assert.equal(state.loading, false);
});

test('late search responses cannot replace a newer result', async () => {
    const pending = [];
    const state = picker(() => new Promise(resolve => pending.push(resolve)));
    state.search = 'Old';
    const old = state.lookup();
    state.search = 'New'; state.edited();
    const latest = state.lookup();
    pending[1]({ok:true,json:async()=>({results:[{id:'new',label:'New station'}]})});
    await latest;
    pending[0]({ok:true,json:async()=>({results:[{id:'old',label:'Old station'}]})});
    await old;
    assert.equal(state.results[0].id, 'new');
    assert.equal(state.loading, false);
});

test('clearing while a lookup is pending leaves no selectable stale result', async () => {
    let respond;
    const state = picker(() => new Promise(resolve => {respond=resolve;}));
    state.search = 'Station';
    const pending = state.lookup();
    state.clear();
    respond({ok:true,json:async()=>({results:[{id:'old',label:'Old station'}]})});
    await pending;
    assert.equal(state.results.length, 0);
    assert.equal(state.id, '');
    assert.equal(state.open, false);
});

test('short input makes no request and failed requests expose an error', async () => {
    let calls = 0;
    const state = picker(async()=>{calls++; return {ok:false};});
    state.search = 'S'; await state.lookup();
    assert.equal(calls, 0);
    state.search = 'Station'; await state.lookup();
    assert.equal(calls, 1);
    assert.match(state.error, /unavailable/);
    assert.equal(state.loading, false);
    assert.equal(state.id, '');
});
