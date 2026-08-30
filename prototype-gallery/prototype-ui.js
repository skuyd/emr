(function wirePrototype() {
  const views = [...document.querySelectorAll('[data-view]')];
  const navItems = [...document.querySelectorAll('[data-nav]')];
  const fileInput = document.querySelector('[data-file-input]');
  const toast = document.querySelector('[data-toast]');
  let toastTimer;

  function announce(message) {
    if (!toast) return;
    window.clearTimeout(toastTimer);
    toast.textContent = message;
    toast.classList.add('is-visible');
    toastTimer = window.setTimeout(() => toast.classList.remove('is-visible'), 3200);
  }

  function currentView() {
    return views.find((view) => !view.hidden)?.dataset.view || 'home';
  }

  function showView(requested) {
    const next = window.PHRPrototype.nextView(currentView(), requested);
    views.forEach((view) => {
      const active = view.dataset.view === next;
      view.hidden = !active;
      view.classList.toggle('is-active', active);
    });
    navItems.forEach((item) => {
      const active = item.dataset.nav === next || (next === 'detail' && item.dataset.nav === 'records');
      item.classList.toggle('is-active', active);
      if (active) item.setAttribute('aria-current', 'page');
      else item.removeAttribute('aria-current');
    });
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  navItems.forEach((item) => item.addEventListener('click', () => showView(item.dataset.nav)));
  document.querySelectorAll('[data-open-detail]').forEach((item) => item.addEventListener('click', () => showView('detail')));

  document.querySelectorAll('[data-search]').forEach((input) => {
    input.addEventListener('input', () => {
      const scope = input.closest('[data-view]') || document;
      const rows = [...scope.querySelectorAll('[data-record]')];
      const documents = rows.map((row) => ({
        title: row.dataset.title,
        institution: row.dataset.institution,
        date: row.dataset.date,
        status: row.dataset.status,
        row,
      }));
      const matches = new Set(window.PHRPrototype.filterDocuments(documents, input.value).map((item) => item.row));
      rows.forEach((row) => { row.hidden = !matches.has(row); });
      const empty = scope.querySelector('[data-empty]');
      if (empty) empty.hidden = matches.size !== 0;
      const count = scope.querySelector('[data-result-count]');
      if (count) count.textContent = `${matches.size} 份资料`;
    });
  });

  document.querySelectorAll('[data-upload]').forEach((button) => {
    button.addEventListener('click', () => fileInput?.click());
  });

  fileInput?.addEventListener('change', () => {
    const count = fileInput.files?.length || 0;
    if (count > 0) announce(`${count} 份原件已加入上传队列，保存后会自动整理`);
  });

  document.querySelectorAll('[data-feedback]').forEach((button) => {
    button.addEventListener('click', () => {
      button.textContent = '已记录，谢谢反馈';
      button.disabled = true;
      announce('反馈已记录，不需要你手动填写正确结果');
    });
  });

  document.querySelectorAll('[data-retry]').forEach((button) => {
    button.addEventListener('click', () => {
      button.textContent = '正在重试…';
      button.disabled = true;
      announce('原件仍在，系统正在重新整理');
    });
  });

  showView(window.PHRPrototype.viewFromSearch(window.location.search));
})();
