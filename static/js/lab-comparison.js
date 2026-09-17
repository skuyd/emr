(() => {
  'use strict';
  const root = document.querySelector('[data-comparison-page]');
  if (!root) return;
  const key = `phr:comparison:${root.dataset.patient}:${root.dataset.stateKey}`;
  const trendKey = `phr:comparison-trends:${root.dataset.patient}`;
  const read = (name, fallback) => { try { return JSON.parse(sessionStorage.getItem(name)) ?? fallback; } catch (_) { return fallback; } };
  const write = (name, value) => { try { sessionStorage.setItem(name, JSON.stringify(value)); } catch (_) { /* Storage is optional. */ } };
  const state = read(key, {});
  const picker = root.querySelector('.comparison-category-picker');
  const boxes = [...picker.querySelectorAll('input[type=checkbox]')];
  function categorySummary() {
    const labels = boxes.filter(box => box.checked).map(box => box.dataset.label);
    picker.querySelector('[data-category-summary]').textContent = labels.length ? labels.slice(0, 2).join('、') + (labels.length > 2 ? ` 等 ${labels.length} 组` : '') : '全部分组';
  }
  boxes.forEach(box => box.addEventListener('change', categorySummary));
  picker.querySelector('[data-clear-categories]').addEventListener('click', () => { boxes.forEach(box => { box.checked = false; }); categorySummary(); });
  picker.addEventListener('keydown', event => {
    if (event.key === 'Escape') { picker.open = false; picker.querySelector('summary').focus(); }
  });
  categorySummary();
  const scroll = root.querySelector('.labs-table-scroll');
  const header = root.querySelector('[data-comparison-header]');
  const toggle = root.querySelector('[data-show-trends]');
  toggle.checked = read(trendKey, false) === true;
  function trends() {
    root.querySelectorAll('[data-trend-column]').forEach(cell => { cell.hidden = !toggle.checked; });
    write(trendKey, toggle.checked);
    positionHeader();
  }
  toggle.addEventListener('change', trends);
  const groups = [...root.querySelectorAll('[data-group]')];
  groups.forEach(group => {
    const button = group.querySelector('[data-group-toggle]');
    function expand(open) {
      button.setAttribute('aria-expanded', String(open));
      group.querySelectorAll('.comparison-indicator').forEach(row => { row.hidden = !open; });
    }
    expand(state.groups?.[group.dataset.group] !== false);
    button.addEventListener('click', () => { expand(button.getAttribute('aria-expanded') !== 'true'); positionHeader(); save(); });
  });
  root.querySelectorAll('.comparison-institution').forEach(button => button.addEventListener('click', () => {
    button.setAttribute('aria-expanded', String(button.getAttribute('aria-expanded') !== 'true'));
    positionHeader();
  }));
  // Move the table's own header inside its scroll plane; never clone a header.
  function positionHeader() {
    if (!header || !scroll) return;
    const rect = scroll.getBoundingClientRect();
    const nav = document.querySelector('.app-header')?.getBoundingClientRect();
    const top = Math.max(0, nav?.bottom || 0);
    const height = header.getBoundingClientRect().height;
    const offset = Math.max(0, Math.min(top - rect.top, rect.height - height));
    header.style.transform = `translateY(${offset}px)`;
    scroll.style.scrollMarginTop = `${top + height + 12}px`;
  }
  function save() {
    const focused = document.activeElement;
    write(key, { x: scroll?.scrollLeft || 0, y: window.scrollY,
      focus: focused?.classList.contains('comparison-value') ? focused.id : state.focus,
      indicator: focused?.closest('[data-indicator]')?.dataset.indicator || state.indicator,
      groups: Object.fromEntries(groups.map(group => [group.dataset.group, group.querySelector('button').getAttribute('aria-expanded') === 'true'])) });
  }
  if (scroll && header) {
    scroll.addEventListener('keydown', event => {
      if (event.target !== scroll) return;
      if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
        event.preventDefault(); scroll.scrollLeft += event.key === 'ArrowRight' ? 100 : -100;
      }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault(); window.scrollBy(0, event.key === 'ArrowDown' ? 100 : -100);
      }
    });
    scroll.addEventListener('focusin', event => {
      const target = event.target;
      if (target === scroll || header.contains(target)) return;
      const bounds = target.getBoundingClientRect();
      const bottom = header.getBoundingClientRect().bottom;
      if (bounds.top < bottom + 8) window.scrollBy(0, bounds.top - bottom - 8);
    });
  }
  window.addEventListener('scroll', positionHeader, { passive: true });
  window.addEventListener('resize', positionHeader);
  if (header) new ResizeObserver(positionHeader).observe(header);
  root.addEventListener('click', event => { if (event.target.closest('a')) save(); });
  window.addEventListener('pagehide', save);
  trends();
  function restore() {
    const saved = read(key, {});
    if (scroll) scroll.scrollLeft = saved.x || 0;
    const navigation = performance.getEntriesByType('navigation')[0]?.type;
    if (navigation === 'back_forward' || navigation === 'reload' || location.hash === '#comparison-results') {
      let target = saved.focus && document.getElementById(saved.focus);
      if (!target && saved.indicator) target = [...root.querySelectorAll('[data-indicator]')].find(row => row.dataset.indicator === saved.indicator)?.querySelector('th');
      if (target) { if (!target.hasAttribute('tabindex') && target.tagName !== 'A') target.tabIndex = -1; target.focus({ preventScroll: true }); }
      else if (scroll) scroll.focus({ preventScroll: true });
      window.scrollTo(0, saved.y || 0);
    }
    positionHeader();
  }
  requestAnimationFrame(restore);
  window.addEventListener('pageshow', restore);
})();
