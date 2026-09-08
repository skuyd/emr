(function () {
  "use strict";
  document.addEventListener("click", function (event) {
    const button = event.target.closest("[data-add-pathology-group]");
    if (!button) return;
    const container = button.closest("[data-pathology-value-form]");
    const countInput = container.querySelector('input[name$="node_count"]');
    const count = Number(countInput.value);
    const message = container.querySelector("[data-pathology-group-status]");
    if (!Number.isInteger(count) || count < 1 || count >= 100) {
      message.textContent = "每个字段最多保留100个原文分组。";
      return;
    }
    const oldName = "group_" + count + "_";
    const newName = "group_" + (count + 1) + "_";
    const additions = ["label", "sampled", "positive", "raw"].map(function (key) {
      const previous = container.querySelector('[name$="' + oldName + key + '"]');
      if (!previous) return null;
      const copy = previous.closest("p").cloneNode(true);
      copy.querySelectorAll("[name], [id], [for]").forEach(function (element) {
        ["name", "id", "for"].forEach(function (attribute) {
          if (element.hasAttribute(attribute)) element.setAttribute(attribute, element.getAttribute(attribute).replace(oldName, newName));
        });
      });
      copy.querySelectorAll("input, textarea").forEach(function (input) {
        input.value = "";
        input.removeAttribute("aria-invalid");
        input.removeAttribute("aria-describedby");
      });
      copy.querySelector("label").textContent = copy.querySelector("label").textContent.replace(/第\d+组/, "第" + (count + 1) + "组");
      return copy;
    });
    if (additions.some(function (item) { return item === null; })) return;
    additions.forEach(function (item) { container.insertBefore(item, button); });
    countInput.value = String(count + 1);
    message.textContent = "已添加第" + (count + 1) + "组；未提供的计数请留空。";
    additions[0].querySelector("input").focus();
    if (count + 1 === 100) button.disabled = true;
  });
})();
