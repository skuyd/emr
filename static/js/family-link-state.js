(function () {
  "use strict";
  function expire(key) {
    try {
      const stored = sessionStorage.getItem(key);
      if (!stored) return;
      let entry;
      try { entry = JSON.parse(stored); } catch (_) { sessionStorage.removeItem(key); return; }
      const remaining = entry && entry.expiry - Date.now();
      if (!entry || !/^[A-Za-z0-9_-]{43}$/.test(entry.token) || !Number.isFinite(entry.expiry)
          || remaining <= 0 || remaining > 10 * 60 * 1000) {
        sessionStorage.removeItem(key);
        return;
      }
      // Keep only the key in the timer. A replacement link may have a later
      // deadline, so reread it instead of clearing a newer token blindly.
      window.setTimeout(function () { expire(key); }, remaining);
    } catch (_) { /* Browser session storage can be disabled. */ }
  }
  expire("phr:pending-invitation");
  expire("phr:pending-share");
})();
