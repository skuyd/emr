(function attachPrototypeCore(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.PHRPrototype = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function createPrototypeCore() {
  const views = new Set(['home', 'records', 'detail']);
  const statuses = ['处理中', '已整理', '仅原件', '处理失败'];

  function filterDocuments(documents, query) {
    const normalized = String(query || '').trim().toLocaleLowerCase('zh-CN');
    if (!normalized) return documents.slice();
    return documents.filter((document) =>
      [document.title, document.institution, document.date, document.status]
        .filter(Boolean)
        .some((value) => String(value).toLocaleLowerCase('zh-CN').includes(normalized)),
    );
  }

  function nextView(current, requested) {
    return views.has(requested) ? requested : current;
  }

  function viewFromSearch(search) {
    const query = new URLSearchParams(String(search || '').replace(/^\?/, ''));
    const requested = query.get('view');
    return views.has(requested) ? requested : 'home';
  }

  function summarizeStatuses(documents) {
    const summary = Object.fromEntries(statuses.map((status) => [status, 0]));
    for (const document of documents) {
      if (Object.hasOwn(summary, document.status)) summary[document.status] += 1;
    }
    return summary;
  }

  return Object.freeze({ filterDocuments, nextView, viewFromSearch, summarizeStatuses });
});
