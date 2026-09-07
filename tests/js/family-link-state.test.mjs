import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../static/js/family-link-state.js', import.meta.url), 'utf8');
const secret = 'a'.repeat(43);

function environment(entries) {
  const storage = new Map(Object.entries(entries));
  const timers = [];
  const clock = {now: 1000};
  vm.runInNewContext(source, {
    Date: {now: () => clock.now},
    sessionStorage: {getItem: (key) => storage.get(key) ?? null, removeItem: (key) => storage.delete(key)},
    window: {setTimeout: (callback, delay) => timers.push({callback, delay})},
  });
  return {storage, timers, clock};
}

test('login pages clear expired and malformed link state without touching other session state', () => {
  const {storage, timers} = environment({
    'phr:pending-invitation': JSON.stringify({token: secret, expiry: 999}),
    'phr:pending-share': '{malformed',
    unrelated: 'preserved',
  });
  assert.deepEqual([...storage.entries()], [['unrelated', 'preserved']]);
  assert.equal(timers.length, 0);
});

test('temporary state expires while the user stays on a login page', () => {
  const {storage, timers, clock} = environment({'phr:pending-share': JSON.stringify({token: secret, expiry: 2000})});
  assert.equal(timers[0].delay, 1000);
  clock.now = 2000;
  timers[0].callback();
  assert.equal(storage.has('phr:pending-share'), false);
});

test('an earlier timer does not erase a freshly opened replacement link', () => {
  const {storage, timers, clock} = environment({'phr:pending-share': JSON.stringify({token: secret, expiry: 2000})});
  storage.set('phr:pending-share', JSON.stringify({token: 'b'.repeat(43), expiry: 3000}));
  clock.now = 2000;
  timers[0].callback();
  assert.equal(storage.has('phr:pending-share'), true);
  assert.equal(timers[1].delay, 1000);
});
