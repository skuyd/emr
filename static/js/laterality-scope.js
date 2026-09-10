(function () {
  "use strict";
  const root = document.querySelector("[data-scope-form]");
  if (!root) return;
  const add = root.querySelector("[data-scope-add]");
  const total = root.querySelector('[name="members-TOTAL_FORMS"]');
  const container = root.querySelector("[data-scope-members]");
  const template = root.querySelector("[data-scope-template]");
  const status = root.querySelector("[data-scope-status]");
  if (!add || !total || !container || !template || !status) return;
  add.addEventListener("click", function () {
    const index = Number(total.value);
    if (!Number.isInteger(index) || index < 0 || index >= 32) {
      status.textContent = "每个字段最多列明 32 个部位。";
      return;
    }
    const fragment = template.content.cloneNode(true);
    fragment.querySelectorAll("*").forEach(function (element) {
      ["name", "id", "for", "aria-describedby"].forEach(function (attribute) {
        if (element.hasAttribute(attribute)) {
          element.setAttribute(attribute, element.getAttribute(attribute).replaceAll("__prefix__", String(index)));
        }
      });
    });
    container.appendChild(fragment);
    total.value = String(index + 1);
    status.textContent = "已增加一个列明部位，请选择对应原件来源。";
  });
})();
