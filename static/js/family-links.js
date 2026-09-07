(function () {
  "use strict";

  document.querySelectorAll("[data-copy-link]").forEach(function (button) {
    button.addEventListener("click", async function () {
      const input = document.getElementById(button.dataset.copyLink);
      const status = button.parentElement.querySelector("[data-copy-status]");
      input.focus();
      input.select();
      try {
        await navigator.clipboard.writeText(input.value);
        status.textContent = "已复制。";
      } catch (_) {
        status.textContent = "链接已选中，请复制后发给对方。";
      }
    });
  });

  const root = document.querySelector("[data-family-token]");
  if (!root) return;
  const storageKey = "phr:pending-" + root.dataset.familyToken;
  const status = root.querySelector("[data-token-status]");
  const login = root.querySelector("[data-token-login]");
  const accept = root.querySelector("[data-token-accept]");
  const csrf = root.querySelector("[name=csrfmiddlewaretoken]").value;
  let token = "";
  let expiry = 0;

  function clear() {
    token = "";
    expiry = 0;
    try { sessionStorage.removeItem(storageKey); } catch (_) { /* optional storage */ }
    if (accept) accept.hidden = true;
  }

  function expired() {
    clear();
    status.textContent = "链接暂存已过期，请重新打开原链接。";
  }

  try {
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    const received = fragment.get("token");
    history.replaceState(null, "", window.location.pathname);
    if (received && /^[A-Za-z0-9_-]{43}$/.test(received)) {
      token = received;
      expiry = Date.now() + 10 * 60 * 1000;
      sessionStorage.setItem(storageKey, JSON.stringify({token: token, expiry: expiry}));
    } else {
      const pending = JSON.parse(sessionStorage.getItem(storageKey) || "null");
      if (pending && /^[A-Za-z0-9_-]{43}$/.test(pending.token) && Number.isFinite(pending.expiry)) {
        token = pending.token;
        expiry = pending.expiry;
      }
    }
  } catch (_) {
    // A blocked browser store still allows accepting in this page after login.
  }

  async function post(url) {
    if (!token || Date.now() >= expiry) { expired(); return null; }
    const response = await fetch(url, {method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {"Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": csrf},
      body: new URLSearchParams({token: token})});
    const data = await response.json();
    if (response.status === 401) {
      login.hidden = false;
      status.textContent = root.dataset.familyToken === "invitation" ? "请使用邀请指定的手机号登录。" : "请先登录后查看分享。";
      return null;
    }
    if (!response.ok) {
      if (response.status === 410 || data.consumed) clear();
      status.textContent = data.error || "暂时无法继续，请稍后重试。";
      return null;
    }
    return data;
  }

  async function inspect() {
    if (!token || Date.now() >= expiry) { expired(); return; }
    window.setTimeout(expired, Math.max(0, expiry - Date.now()));
    if (root.dataset.authenticated !== "true") {
      login.hidden = false;
      status.textContent = root.dataset.familyToken === "invitation" ? "请使用邀请指定的手机号登录。" : "请先登录后查看分享。";
      return;
    }
    try {
      const data = await post(root.dataset.inspectUrl);
      if (!data) return;
      if (root.dataset.familyToken === "share") {
        clear();
        window.location.assign(data.redirect);
        return;
      }
      root.querySelector("[data-invitation-summary]").textContent = "加入“" + data.patient_name + "”的档案，角色：" + data.role + "。";
      status.textContent = "请确认是否接受此邀请。";
      accept.hidden = false;
    } catch (_) { status.textContent = "连接失败，请刷新后重试。"; }
  }

  if (accept) accept.addEventListener("click", async function () {
    accept.disabled = true;
    try {
      const data = await post(root.dataset.acceptUrl);
      if (data) {
        clear();
        window.location.assign(data.redirect);
      }
    } catch (_) { status.textContent = "连接失败，请稍后重试。"; }
    accept.disabled = false;
  });
  inspect();
})();
