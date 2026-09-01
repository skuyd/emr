document.addEventListener("DOMContentLoaded", () => {
  const code = document.querySelector("#id_code");
  if (code) code.focus();

  document.querySelectorAll("[data-password-toggle]").forEach((toggle) => {
    const input = document.getElementById(toggle.getAttribute("aria-controls"));
    if (!input) return;
    toggle.addEventListener("click", () => {
      const visible = input.type === "text";
      input.type = visible ? "password" : "text";
      toggle.setAttribute("aria-pressed", String(!visible));
      toggle.textContent = visible ? "显示密码" : "隐藏密码";
    });
  });
});
