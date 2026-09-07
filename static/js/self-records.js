(() => {
  "use strict";
  const form = document.querySelector("[data-record-new-time]");
  if (!form) return;
  try {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const now = new Date();
    const pad = (value) => String(value).padStart(2, "0");
    form.elements.timezone.value = zone;
    form.elements.measured_local.value = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`;
  } catch (_) {
    // The visible server time and time zone remain editable without JavaScript.
  }
})();
