(() => {
  "use strict";
  const selectors = document.querySelectorAll("[data-report-source]");
  const highlight = document.querySelector("[data-report-highlight]");
  const sourceData = document.querySelector("#report-sources");
  if (!selectors.length || !highlight || !sourceData) return;
  const sources = JSON.parse(sourceData.textContent);
  function showSource(selector) {
    const source = selector.value === "" ? null : sources[Number(selector.value)];
    highlight.hidden = !source;
    if (!source) return;
    const xs = source.polygon.map(point => point[0]);
    const ys = source.polygon.map(point => point[1]);
    highlight.style.left = `${Math.min(...xs) * 100}%`;
    highlight.style.top = `${Math.min(...ys) * 100}%`;
    highlight.style.width = `${(Math.max(...xs) - Math.min(...xs)) * 100}%`;
    highlight.style.height = `${(Math.max(...ys) - Math.min(...ys)) * 100}%`;
  }
  selectors.forEach(selector => {
    selector.addEventListener("change", () => showSource(selector));
    selector.addEventListener("focus", () => showSource(selector));
  });
  showSource(selectors[0]);
})();
