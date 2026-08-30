(function () {
  "use strict";

  const form = document.querySelector("[data-notification-form]");
  if (!form) return;
  const desired = form.querySelector("[data-notification-enabled]");
  const prompted = form.querySelector("[data-notification-prompted]");
  const permission = form.querySelector("[data-notification-permission]");
  const browserFamily = form.querySelector("[data-browser-family]");
  const status = form.querySelector("[data-notification-status]");
  const vapidPublicKey = form.dataset.vapidPublicKey;
  const userAgent = navigator.userAgent;
  browserFamily.value = /Edg\//.test(userAgent) ? "edge" : (/Chrome\//.test(userAgent) ? "chrome" : (/Safari\//.test(userAgent) ? "safari" : "other"));

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    try {
      if (desired.value === "true") {
        const enabled = await enableNotifications();
        desired.value = enabled ? "true" : "false";
      } else {
        status.textContent = "正在关闭通知…";
        await disableNotifications();
      }
      form.submit();
    } catch (_error) {
      status.textContent = "当前无法更新通知设置，请稍后重试。";
    }
  });

  function applicationServerKey(value) {
    const padding = "=".repeat((4 - value.length % 4) % 4);
    const decoded = atob((value + padding).replaceAll("-", "+").replaceAll("_", "/"));
    return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
  }

  function csrfToken() {
    const input = document.querySelector("input[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  async function postJson(url, payload) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
      body: JSON.stringify(payload),
    });
  }

  async function disableNotifications() {
    if (!("serviceWorker" in navigator)) return;
    const registration = await navigator.serviceWorker.getRegistration("/");
    const subscription = registration && await registration.pushManager.getSubscription();
    const endpoint = subscription ? subscription.endpoint : null;
    const response = await postJson("/api/push-subscriptions/revoke/", endpoint ? { endpoint } : {});
    if (!response.ok) throw new Error("revoke_failed");
    if (subscription) await subscription.unsubscribe();
  }

  async function enableNotifications() {
    if (!("Notification" in window) || !("serviceWorker" in navigator) || !("PushManager" in window) || !vapidPublicKey) {
      throw new Error("unsupported");
    }
    let result = Notification.permission;
    if (result === "default") {
      status.textContent = "正在等待浏览器授权…";
      result = await Notification.requestPermission();
      prompted.value = "true";
    }
    permission.value = result;
    if (result !== "granted") return false;
    const registration = await navigator.serviceWorker.register("/service-worker.js", { scope: "/" });
    let subscription = await registration.pushManager.getSubscription();
    if (!subscription) {
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: applicationServerKey(vapidPublicKey),
      });
    }
    const value = subscription.toJSON();
    const response = await postJson("/api/push-subscriptions/", {
      endpoint: value.endpoint,
      keys: value.keys,
      browser_family: browserFamily.value,
    });
    if (!response.ok) {
      await subscription.unsubscribe();
      throw new Error("subscription_failed");
    }
    return true;
  }

}());
