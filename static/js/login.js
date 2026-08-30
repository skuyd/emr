document.addEventListener("DOMContentLoaded", () => {
  const button = document.querySelector("[data-request-accepted]");
  if (!button) return;

  let remaining = 60;
  button.disabled = true;
  const update = () => { button.textContent = `${remaining} 秒后可重新获取`; };
  update();
  const timer = window.setInterval(() => {
    remaining -= 1;
    if (remaining <= 0) {
      window.clearInterval(timer);
      button.disabled = false;
      button.removeAttribute("data-request-accepted");
      button.textContent = "获取验证码";
      return;
    }
    update();
  }, 1000);
});
