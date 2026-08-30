"use strict";

const ALLOWED_COPY = new Map([
  ["资料整理完成", "你上传的资料已整理完成，点击查看结果。"],
  ["资料整理有未完成项目", "你上传的资料中有未完成项目，点击查看任务状态。"],
]);
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let payload;
  try {
    payload = event.data.json();
  } catch (_error) {
    return;
  }
  if (!payload || Object.keys(payload).sort().join(",") !== "body,notification_id,title") return;
  if (!UUID_PATTERN.test(payload.notification_id)) return;
  if (ALLOWED_COPY.get(payload.title) !== payload.body) return;
  event.waitUntil(self.registration.showNotification(payload.title, {
    body: payload.body,
    tag: `family-phr-task-${payload.notification_id}`,
    data: { notificationId: payload.notification_id },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const notificationId = event.notification.data && event.notification.data.notificationId;
  if (!UUID_PATTERN.test(notificationId || "")) return;
  const openUrl = `/notifications/${notificationId}/open/`;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windows) {
      if ("navigate" in client) await client.navigate(openUrl);
      return client.focus();
    }
    return self.clients.openWindow(openUrl);
  })());
});
