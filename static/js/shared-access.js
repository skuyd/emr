(function () {
  "use strict";
  const root = document.querySelector("[data-share-status-url]");
  if (!root) return;
  let pending = false;
  let unavailable = false;
  async function check() {
    if (pending || unavailable || document.hidden) return;
    pending = true;
    try {
      const response = await fetch(root.dataset.shareStatusUrl, {credentials: "same-origin", cache: "no-store"});
      if (!response.ok || response.redirected) {
        unavailable = true;
        const content = root.querySelector("[data-share-content]");
        content.textContent = "分享已失效，请联系分享人重新生成。";
        content.setAttribute("role", "alert");
        window.clearInterval(timer);
      }
    } catch (_) { /* Every actual source request still checks live access. */ }
    pending = false;
  }
  const timer = window.setInterval(check, 15000);
  document.addEventListener("visibilitychange", check);
  window.addEventListener("pageshow", check);
})();
