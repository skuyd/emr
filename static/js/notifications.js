(() => {
  "use strict";

  const center = document.querySelector("[data-notification-center]");
  if (!center) return;
  const toggle = center.querySelector("[data-notification-toggle]");
  const panel = center.querySelector("[data-notification-panel]");
  const list = center.querySelector("[data-notification-list]");
  const badge = center.querySelector("[data-notification-badge]");
  const toast = center.querySelector("[data-notification-toast]");
  const known = new Set(
    Array.from(list.querySelectorAll("[data-notification-id]"), (item) => item.dataset.notificationId)
  );
  let toastTimer = null;

  function renderBadge(count) {
    badge.textContent = String(count);
    badge.hidden = count === 0;
    toggle.setAttribute("aria-label", count ? `任务通知，${count} 条未读` : "任务通知，无未读");
  }

  function notificationItem(item) {
    const row = document.createElement("li");
    row.dataset.notificationId = item.notification_id;
    const link = document.createElement("a");
    link.href = `/notifications/${item.notification_id}/open/`;
    const title = document.createElement("strong");
    title.textContent = item.title;
    const body = document.createElement("span");
    body.textContent = item.body;
    link.append(title, body);
    row.append(link);
    return row;
  }

  function renderList(items) {
    list.replaceChildren();
    if (!items.length) {
      const empty = document.createElement("li");
      empty.dataset.notificationEmpty = "";
      empty.textContent = "暂无通知。";
      list.append(empty);
      return;
    }
    for (const item of items.slice(0, 5)) list.append(notificationItem(item));
  }

  function showToast(item) {
    toast.replaceChildren();
    const title = document.createElement("strong");
    title.textContent = item.title;
    const body = document.createElement("span");
    body.textContent = item.body;
    toast.append(title, body);
    toast.hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => { toast.hidden = true; }, 7000);
  }

  async function refresh({ announce = false } = {}) {
    try {
      const response = await fetch("/api/notifications/", {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) return;
      const payload = await response.json();
      const fresh = payload.notifications.find((item) => !item.read && !known.has(item.notification_id));
      renderBadge(payload.unread_count);
      renderList(payload.notifications);
      for (const item of payload.notifications) known.add(item.notification_id);
      if (fresh && announce) showToast(fresh);
    } catch (_error) {
      // A missed poll is harmless; the durable station notification remains unread.
    }
  }

  toggle.addEventListener("click", () => {
    const willOpen = panel.hidden;
    panel.hidden = !willOpen;
    toggle.setAttribute("aria-expanded", String(willOpen));
    if (willOpen) refresh();
  });
  document.addEventListener("click", (event) => {
    if (!panel.hidden && !center.contains(event.target)) {
      panel.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
    }
  });
  window.addEventListener("phr:task-terminal", () => refresh({ announce: true }));
  const pollTimer = window.setInterval(() => {
    if (!document.hidden) refresh({ announce: true });
  }, 15000);
  window.addEventListener("pagehide", () => {
    window.clearInterval(pollTimer);
    window.clearTimeout(toastTimer);
  });
})();
