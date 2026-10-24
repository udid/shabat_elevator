import test from 'node:test';
import assert from 'node:assert/strict';
import { createPresence } from '../web/presence.js';

function storage() {
  const values = new Map();
  return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
}
const url = 'https://api.example/api/state';
const randomBytes = (bytes) => bytes.fill(42);

test('visible tabs share a persistent browser id within the building', () => {
  const saved = storage();
  const options = { getStorage: () => saved, randomBytes, getVisibility: () => 'visible' };
  const first = createPresence('building-a', options);
  const second = createPresence('building-a', { ...options, randomBytes: () => { throw new Error('Must reuse'); } });
  assert.equal(first(url), `${url}?visitor=${'2a'.repeat(16)}`);
  assert.equal(second(url), first(url));
  assert.equal(first(`${url}?existing=1`), `${url}?existing=1&visitor=${'2a'.repeat(16)}`);
});

test('hidden and unknown visibility never send or create an id', () => {
  for (const visibility of ['hidden', 'prerender', undefined]) {
    let reads = 0;
    const presence = createPresence('a', { getVisibility: () => visibility, getStorage: () => { reads += 1; throw new Error(); } });
    assert.equal(presence(url), url);
    assert.equal(reads, 0);
  }
});

test('visibility is checked for each poll and hidden tabs do not erase shared ids', () => {
  let visible = true;
  const saved = storage();
  const presence = createPresence('a', { getStorage: () => saved, randomBytes, getVisibility: () => visible ? 'visible' : 'hidden' });
  const counted = presence(url);
  visible = false;
  assert.equal(presence(url), url);
  visible = true;
  assert.equal(presence(url), counted);
});

test('storage and crypto failures cannot stop elevator requests', () => {
  const failing = () => { throw new Error('Blocked'); };
  const cases = [
    { getStorage: failing },
    { getStorage: () => ({ getItem: failing }) },
    { getStorage: () => ({ getItem: () => null, setItem: failing }), randomBytes },
    { getStorage: () => storage(), randomBytes: failing },
    { getStorage: () => storage(), randomBytes: () => [] },
  ];
  for (const options of cases) {
    assert.equal(createPresence('a', { ...options, getVisibility: () => 'visible' })(url), url);
  }
});

test('invalid persisted ids are replaced and building namespaces are distinct', () => {
  const saved = storage();
  saved.setItem('shabat-elevator:a:visitor', 'invalid&extra=true');
  const options = { getStorage: () => saved, randomBytes, getVisibility: () => 'visible' };
  assert.equal(createPresence('a', options)(url), `${url}?visitor=${'2a'.repeat(16)}`);
  const different = createPresence('b', { ...options, randomBytes: (bytes) => bytes.fill(10) })(url);
  assert.equal(different, `${url}?visitor=${'0a'.repeat(16)}`);
  saved.setItem('shabat-elevator:a:visitor', 'b'.repeat(32));
  assert.equal(createPresence('a', options)(url), `${url}?visitor=${'b'.repeat(32)}`);
});
