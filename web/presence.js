// Presence is optional: a browser that cannot persist a random ID is omitted
// rather than counted as a new visitor on every poll or in each tab.
export function createPresence(buildingId, {
  getStorage = () => globalThis.localStorage,
  randomBytes = (bytes) => globalThis.crypto.getRandomValues(bytes),
  getVisibility = () => globalThis.document.visibilityState,
} = {}) {
  const key = `shabat-elevator:${buildingId}:visitor`;
  const valid = (value) => typeof value === 'string' && /^[a-f0-9]{32}$/.test(value);
  return (url) => {
    try {
      if (getVisibility() !== 'visible') return url;
      const storage = getStorage();
      let visitor = storage.getItem(key);
      if (!valid(visitor)) {
        visitor = Array.from(randomBytes(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, '0')).join('');
        if (!valid(visitor)) return url;
        storage.setItem(key, visitor);
        // Re-read so tabs converge on the currently shared value.
        visitor = storage.getItem(key);
      }
      if (!valid(visitor)) return url;
      return `${url}${url.includes('?') ? '&' : '?'}visitor=${visitor}`;
    } catch { return url; }
  };
}
