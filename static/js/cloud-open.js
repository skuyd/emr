(() => {
  "use strict";
  const form = document.querySelector("[data-cloud-open-form]");
  const error = document.querySelector("[data-cloud-open-error]");
  if (!form || !error) return;
  const button = form.querySelector("button[type=submit]");
  button.disabled = false;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (button.disabled) return;
    error.hidden = true;
    // Create only an empty same-origin window during the user gesture. Its
    // opener is removed before any asynchronous work or external navigation.
    const destination = window.open("about:blank", "_blank");
    if (!destination) {
      error.textContent = "浏览器阻止了新窗口，请允许后重试。";
      error.hidden = false;
      return;
    }
    destination.opener = null;
    const policy = destination.document.createElement("meta");
    policy.name = "referrer";
    policy.content = "no-referrer";
    destination.document.head.appendChild(policy);
    destination.document.title = "正在核对来源";
    destination.document.body.textContent = "正在核对来源，请稍候。";
    button.disabled = true;
    try {
      const endpoint = new URL(form.action, window.location.href);
      if (endpoint.origin !== window.location.origin) throw new Error("invalid_endpoint");
      // CORS-mode same-origin fetch sends the browser's real Origin even when
      // the document sends no Referer. Never follow an external redirect here.
      const response = await fetch(endpoint.href, {
        method: "POST", mode: "cors", credentials: "same-origin", redirect: "error",
        cache: "no-store", referrerPolicy: "no-referrer",
        headers: { "X-Cloud-Open": "navigate" }, body: new FormData(form),
      });
      if (response.status !== 200) throw new Error("source_unavailable");
      const location = response.headers.get("Location");
      if (!location || !/^https?:\/\//i.test(location)) throw new Error("invalid_navigation");
      destination.location.replace(location);
    } catch (_) {
      destination.close();
      error.textContent = "本次未打开外部站点。来源或访问资格可能已变化，请返回来源核对后重试。";
      error.hidden = false;
    } finally {
      button.disabled = false;
    }
  });
})();
