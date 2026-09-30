(() => {
  "use strict";

  const storageKey = "lab-report-review-prototype-v1";
  const fields = ["raw_name", "raw_value", "raw_unit", "reference_range_raw", "report_flag_raw", "specimen", "method_raw"];
  const row = (id, page, name, value, unit, range, y, options = {}) => ({
    id, page, raw_name: name, raw_value: value, raw_unit: unit,
    reference_range_raw: range, report_flag_raw: options.flag || "",
    specimen: options.specimen || "静脉血", method_raw: options.method || "",
    standard_name: options.standard || name, box: [68, y, 864, 78],
    excluded: false, exclusion_reason: "",
  });
  const reports = [
    {
      id: "demo-018", title: "综合检验报告",
      identity: {institution: "云杉模拟检验中心", sampled_at: "2026-09-27 08:40", report_number: "DEMO-LAB-2609-018"},
      sources: [
        {label: "第 1 页 · 血常规", image: "assets/report-page-1.svg"},
        {label: "第 2 页 · 炎症指标", image: "assets/report-page-2.svg"},
      ],
      rows: [
        row("wbc", 0, "白细胞计数", "6.20", "×10⁹/L", "3.50–9.50", 603),
        row("rbc", 0, "红细胞计数", "4.50", "×10¹²/L", "3.80–5.10", 681),
        row("hgb", 0, "血红蛋白", "138", "g/L", "115–150", 759),
        row("plt", 0, "血小板计数", "220", "×10⁹/L", "125–350", 837),
        row("crp", 1, "C 反应蛋白", "5.2", "mg/L", "0–8.0", 603),
        row("esr", 1, "红细胞沉降率", "12", "mm/h", "0–20", 681),
      ],
      added: [], confirmed: false, conflictChoice: "",
    },
    {
      id: "demo-019", title: "内分泌检验报告",
      identity: {institution: "云杉模拟检验中心", sampled_at: "2026-09-28 09:20", report_number: "DEMO-LAB-2609-019"},
      sources: [{label: "第 1 页 · 甲状腺功能", image: "assets/zero-recognition.svg"}],
      rows: [], added: [], confirmed: false, conflictChoice: "",
    },
  ];
  const $ = selector => document.querySelector(selector);
  const clone = value => JSON.parse(JSON.stringify(value));
  const saved = (() => {
    try { return JSON.parse(localStorage.getItem(storageKey) || "{}"); }
    catch { return {}; }
  })();
  let currentIndex = reports.findIndex(report => !saved[report.id]?.confirmed);
  if (currentIndex < 0) currentIndex = 0;
  let scenario = "normal";
  let draft = getDraft();
  let dirty = false;
  let currentPage = 0;
  let zoom = 1;
  let angle = 0;
  let navigationAfterLeave = null;
  let returnTarget = null;

  function getDraft() {
    return clone(saved[reports[currentIndex].id] || reports[currentIndex]);
  }

  function hasConflict() {
    return scenario === "conflict" && currentIndex === 0;
  }

  function unreadableRows() {
    return draft.rows.concat(draft.added).filter(item => item.unreadable && !item.excluded);
  }

  function setFeedback(message, kind = "neutral") {
    const feedback = $("#action-feedback");
    feedback.textContent = message;
    feedback.dataset.kind = kind;
  }

  function updateCounts() {
    const allRows = draft.rows.concat(draft.added);
    const retained = allRows.filter(item => !item.excluded).length;
    const excluded = allRows.length - retained;
    const unresolved = (hasConflict() && !draft.conflictChoice ? 1 : 0) + unreadableRows().length;
    $("#review-counts").textContent = `保留 ${retained} 项 · 排除 ${excluded} 项 · ${draft.sources.length} 页来源 · 待处理 ${unresolved} 项`;
    $("#row-count").textContent = `（${allRows.length}）`;
    $("#zero-results").hidden = allRows.length > 0;
  }

  function updateSummary() {
    $("#report-title").textContent = draft.title;
    $("#summary-institution").textContent = draft.identity.institution || "医院未提供 / 未识别";
    $("#summary-sampled-at").textContent = draft.identity.sampled_at || "采样时间未提供 / 未识别";
    $("#summary-number").textContent = draft.identity.report_number || "报告号未提供 / 未识别";
    $("#summary-pages").textContent = `${draft.sources.length} 页`;
    const badge = $("[data-review-state]");
    const confirmed = draft.confirmed && !dirty && (!hasConflict() || draft.conflictChoice)
      && !unreadableRows().length;
    badge.textContent = draft.confirmed && dirty ? "待重新确认" : confirmed ? "已确认" : "待核对";
    badge.dataset.reviewState = confirmed ? "confirmed" : "pending";
    $(".report-summary").classList.toggle("is-dirty", dirty);
    $(".report-summary").classList.toggle("is-blocked", hasConflict() && !draft.conflictChoice);
    const notice = $("#problem-notice");
    const issues = [];
    if (hasConflict() && !draft.conflictChoice)
      issues.push("仍有 1 项识别冲突。可先保存其他修改；处理冲突前不能确认整份报告。");
    if (unreadableRows().length)
      issues.push("有指标的原件结果为空或无法辨认，已列为待处理，暂不能确认整份报告。");
    if (scenario === "readonly") issues.push("当前为只读权限，可查看报告与原件，不能保存或确认。");
    const message = issues.join(" ");
    notice.textContent = message;
    notice.hidden = !message;
    $("#completion-note").hidden = dirty || (hasConflict() && !draft.conflictChoice)
      || !reports.every(report => saved[report.id]?.confirmed);
    updateCounts();
  }

  function layoutViewer() {
    const viewport = $("#source-viewport");
    const image = $("#source-image");
    if (!image.naturalWidth || !viewport.clientWidth) return;
    const sheet = $("#source-sheet");
    const stage = $("#source-stage");
    const style = getComputedStyle(viewport);
    const baseWidth = viewport.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    sheet.style.width = `${baseWidth}px`;
    const baseHeight = sheet.offsetHeight;
    const turned = angle % 180 !== 0;
    const visualWidth = zoom * (turned ? baseHeight : baseWidth);
    const visualHeight = zoom * (turned ? baseWidth : baseHeight);
    stage.style.width = `${Math.max(baseWidth, visualWidth)}px`;
    stage.style.height = `${visualHeight}px`;
    sheet.style.transform = `translate(-50%, -50%) rotate(${angle}deg) scale(${zoom})`;
    viewport.scrollLeft = 0;
    viewport.scrollTop = 0;
    const highlight = $("#source-highlight");
    if (highlight.hidden) return;
    const sourceRect = viewport.getBoundingClientRect();
    const targetRect = highlight.getBoundingClientRect();
    viewport.scrollLeft += targetRect.left + targetRect.width / 2 - (sourceRect.left + viewport.clientWidth / 2);
    viewport.scrollTop += targetRect.top + targetRect.height / 2 - (sourceRect.top + viewport.clientHeight / 2);
  }

  function showPage(index, box = undefined) {
    currentPage = Number(index);
    const source = draft.sources[currentPage];
    if (!source) return;
    $("#source-content").hidden = false;
    $("#source-toggle").setAttribute("aria-expanded", "true");
    $("#source-toggle").textContent = "收起原图";
    $("#source-image").src = source.image;
    $("#source-image").alt = `${draft.title}，${source.label}，合成演示原件`;
    $("#source-page-label").textContent = source.label;
    for (const tab of $("#source-tabs").querySelectorAll("button")) {
      tab.setAttribute("aria-pressed", String(Number(tab.dataset.page) === currentPage));
    }
    const highlight = $("#source-highlight");
    highlight.hidden = !box;
    if (box) {
      highlight.style.left = `${box[0] / 10}%`;
      highlight.style.top = `${box[1] / 14}%`;
      highlight.style.width = `${box[2] / 10}%`;
      highlight.style.height = `${box[3] / 14}%`;
      $("#source-location").textContent = "已定位原件中的对应指标区域。";
    } else if (box === null) {
      $("#source-location").textContent = "已定位到对应原页；此项没有可靠的精确坐标。";
    } else {
      $("#source-location").textContent = "选择右侧指标，可定位到相应原页。";
    }
    if ($("#source-image").complete && $("#source-image").naturalWidth)
      requestAnimationFrame(layoutViewer);
  }

  function renderSources() {
    const tabs = $("#source-tabs");
    tabs.replaceChildren();
    draft.sources.forEach((source, index) => {
      const tab = document.createElement("button");
      tab.type = "button";
      tab.className = "source-tab";
      tab.dataset.page = String(index);
      tab.textContent = `第 ${index + 1} 页`;
      tab.title = source.label;
      tab.addEventListener("click", () => showPage(index));
      tabs.append(tab);
    });
    currentPage = Math.min(currentPage, draft.sources.length - 1);
    showPage(currentPage);
  }

  function sourceDetail(item, manual) {
    const originalName = reports[currentIndex].rows.find(row => row.id === item.id)?.raw_name;
    const match = manual ? "其他 / 待匹配"
      : item.raw_name === originalName ? item.standard_name : "待重新匹配";
    return `${draft.sources[item.page].label} · ${manual ? "人工转录" : "自动识别"} · 目录匹配：${match}`;
  }

  function createCard(item, manual = false) {
    const card = $("#result-template").content.firstElementChild.cloneNode(true);
    card.dataset.rowId = item.id;
    if (manual) card.dataset.newRow = "";
    card.classList.toggle("is-excluded", item.excluded);
    const title = card.querySelector("[data-locate]");
    title.textContent = `第 ${item.page + 1} 页 · ${item.raw_name || "新补录项目"}`;
    title.addEventListener("click", () => {
      returnTarget = title;
      showPage(item.page, item.box || null);
      if (matchMedia("(max-width: 928px)").matches) {
        $("#return-to-edit").hidden = false;
        $(".source-panel").scrollIntoView({block: "start", behavior: "smooth"});
      }
    });
    card.querySelector("[data-row-state]").textContent = item.excluded ? "已排除" : manual ? "人工补录" : "自动识别";
    for (const field of fields) card.querySelector(`[name="${field}"]`).value = item[field] || "";
    card.querySelector("[data-source-detail]").textContent = sourceDetail(item, manual);
    const exclude = card.querySelector("[data-exclude]");
    const reasonWrap = card.querySelector("[data-reason-wrap]");
    const remove = card.querySelector("[data-remove]");
    const pageWrap = card.querySelector("[data-page-wrap]");
    card.querySelector("[data-issue-wrap]").hidden = false;
    card.querySelector('[name="unreadable"]').checked = Boolean(item.unreadable);
    if (manual) {
      exclude.hidden = true;
      remove.hidden = false;
      pageWrap.hidden = false;
      const sourceSelect = pageWrap.querySelector("select");
      draft.sources.forEach((source, index) => {
        const option = new Option(source.label, String(index));
        sourceSelect.add(option);
      });
      sourceSelect.value = String(item.page);
      remove.addEventListener("click", () => {
        syncForm();
        draft.added = draft.added.filter(other => other.id !== item.id);
        card.remove();
        markDirty();
      });
    } else {
      exclude.textContent = item.excluded ? "恢复此项" : "排除此项";
      reasonWrap.hidden = !item.excluded;
      reasonWrap.querySelector("select").value = item.exclusion_reason || "";
      exclude.addEventListener("click", () => {
        syncForm();
        item.excluded = !item.excluded;
        if (!item.excluded) item.exclusion_reason = "";
        card.classList.toggle("is-excluded", item.excluded);
        card.querySelector("[data-row-state]").textContent = item.excluded ? "已排除" : "自动识别";
        exclude.textContent = item.excluded ? "恢复此项" : "排除此项";
        reasonWrap.hidden = !item.excluded;
        reasonWrap.querySelector("select").value = item.exclusion_reason;
        markDirty();
      });
    }
    if (scenario === "readonly") {
      for (const input of card.querySelectorAll("input")) input.readOnly = true;
      for (const select of card.querySelectorAll("select")) select.disabled = true;
      card.querySelector(".result-actions").hidden = true;
    }
    return card;
  }

  function renderRows() {
    $("#results").replaceChildren(...draft.rows.map(item => createCard(item)));
    $("#added-results").replaceChildren(...draft.added.map(item => createCard(item, true)));
    updateCounts();
  }

  function syncForm() {
    for (const field of ["institution", "sampled_at", "report_number"]) {
      draft.identity[field] = $(`#review-form [name="${field}"]`).value.trim();
    }
    for (const card of $("#review-form").querySelectorAll("[data-row-id]")) {
      const collection = card.hasAttribute("data-new-row") ? draft.added : draft.rows;
      const item = collection.find(candidate => candidate.id === card.dataset.rowId);
      if (!item) continue;
      for (const field of fields) item[field] = card.querySelector(`[name="${field}"]`).value.trim();
      item.unreadable = card.querySelector('[name="unreadable"]').checked;
      if (card.hasAttribute("data-new-row")) {
        item.page = Number(card.querySelector('[name="source_page"]').value);
      } else item.exclusion_reason = card.querySelector('[name="exclusion_reason"]').value;
    }
    draft.conflictChoice = $('#conflict-section input[name="conflict-choice"]:checked')?.value || draft.conflictChoice;
  }

  function markDirty() {
    dirty = true;
    updateSummary();
    setFeedback(draft.confirmed ? "当前编辑尚未保存；保存后需重新确认。" : "有未保存的修改。", "warning");
  }

  function showFieldError(input, message, error) {
    input.setAttribute("aria-invalid", "true");
    error.textContent = message;
    error.hidden = false;
    input.focus();
    input.scrollIntoView({block: "center"});
    setFeedback("请先处理高亮字段，输入会保留在当前页。", "error");
  }

  function validateDraft() {
    for (const card of $("#added-results").querySelectorAll("[data-new-row]")) {
      const item = draft.added.find(candidate => candidate.id === card.dataset.rowId);
      for (const field of ["raw_name", "raw_value"]) {
        const input = card.querySelector(`[name="${field}"]`);
        if (field === "raw_value" && item.unreadable) continue;
        if (!input.value.trim()) {
          showFieldError(input, field === "raw_name" ? "请填写原件上的项目名称。" : "请填写原件上的结果原文。",
            input.parentElement.querySelector("[data-field-error]"));
          return false;
        }
      }
    }
    for (const card of $("#results").querySelectorAll("[data-row-id]")) {
      const item = draft.rows.find(candidate => candidate.id === card.dataset.rowId);
      if (!item.excluded && !item.unreadable && !item.raw_value) {
        const input = card.querySelector('[name="raw_value"]');
        showFieldError(input, "结果原文为空时，请标记待处理。",
          input.parentElement.querySelector("[data-field-error]"));
        return false;
      }
      if (item.excluded && !item.exclusion_reason) {
        showFieldError(card.querySelector('[name="exclusion_reason"]'), "请选择误识别或重复识别。",
          card.querySelector("[data-reason-error]"));
        return false;
      }
    }
    return true;
  }

  function saveCurrent(confirm) {
    syncForm();
    if (!validateDraft()) return false;
    if (confirm && unreadableRows().length) {
      $('#review-form [name="unreadable"]:checked')?.focus();
      setFeedback("仍有原件结果待处理。可保存现有修改，处理后再确认本报告。", "error");
      return false;
    }
    if (confirm && hasConflict() && !draft.conflictChoice) {
      $("#conflict-section").scrollIntoView({block: "center"});
      setFeedback("请先处理第 2 页的识别冲突，再确认本报告。", "error");
      return false;
    }
    if (confirm) draft.confirmed = true;
    else if (dirty) draft.confirmed = false;
    saved[draft.id] = clone(draft);
    localStorage.setItem(storageKey, JSON.stringify(saved));
    dirty = false;
    updateSummary();
    setFeedback(confirm ? "本报告已确认。" : draft.confirmed
      ? "本报告维持已确认，当前没有新修改。" : "修改已保存，本报告仍待核对。", "success");
    return true;
  }

  function loadCurrent() {
    draft = getDraft();
    dirty = false;
    currentPage = 0;
    zoom = 1;
    angle = 0;
    $("#source-sheet").style.transform = "";
    returnTarget = null;
    $("#return-to-edit").hidden = true;
  }

  function renderEmpty() {
    const range = scenario === "range";
    const processing = scenario === "processing";
    $("#empty-title").textContent = range ? "报告范围尚未确定，暂不能确认"
      : processing ? "原图仍在处理中" : "尚无可核对的报告范围";
    $("#empty-description").textContent = range
      ? "这份资料还没有明确的报告单元。请从资料详情处理来源范围；此处不把整份混合文件默认视为一份报告。"
      : processing ? "来源页尚未生成，不能把空指标列表当作识别完成。可先查看下一份已就绪的报告。"
        : "当前没有范围明确的检验报告。此处不能创建报告或整份确认。";
    $("#empty-next").hidden = scenario === "none";
    $("#empty-view").hidden = false;
    $("#report-view").hidden = true;
  }

  function render() {
    $("#demo-scenario").value = scenario;
    if (scenario === "range" || scenario === "processing" || scenario === "none") {
      renderEmpty();
      return;
    }
    $("#empty-view").hidden = true;
    $("#report-view").hidden = false;
    $("#report-position").textContent = `第 ${currentIndex + 1} 份 / 共 ${reports.length} 份`;
    $("#previous-report").disabled = currentIndex === 0;
    $("#next-report").disabled = currentIndex === reports.length - 1;
    for (const field of ["institution", "sampled_at", "report_number"]) {
      const input = $(`#review-form [name="${field}"]`);
      input.value = draft.identity[field];
      input.readOnly = scenario === "readonly";
    }
    $("#conflict-section").hidden = !hasConflict();
    for (const radio of $('#conflict-section').querySelectorAll('input[name="conflict-choice"]')) {
      radio.checked = radio.value === draft.conflictChoice;
      radio.disabled = scenario === "readonly";
    }
    $(".add-section").hidden = scenario === "readonly";
    $("#action-dock").hidden = scenario === "readonly";
    renderSources();
    renderRows();
    updateSummary();
    setFeedback(scenario === "readonly" ? "" : "请逐页对照原件，再保存或确认本报告。");
  }

  function navigate(action) {
    if (!dirty) { action(); return; }
    navigationAfterLeave = action;
    $("#leave-dialog").showModal();
  }

  function goToReport(index) {
    currentIndex = index;
    loadCurrent();
    render();
    window.scrollTo({top: 0, behavior: "instant"});
  }

  $("#review-form").addEventListener("input", event => {
    const label = event.target.closest("label");
    const fieldError = label?.querySelector(".field-error");
    if (fieldError) fieldError.hidden = true;
    event.target.removeAttribute("aria-invalid");
    syncForm();
    if (hasConflict() && event.target.matches('[data-row-id="crp"] [name="raw_value"]')
        && draft.conflictChoice && event.target.value.trim() !== draft.conflictChoice) {
      draft.conflictChoice = "";
      for (const radio of $('#conflict-section').querySelectorAll('input[name="conflict-choice"]')) radio.checked = false;
    }
    if (event.target.name === "raw_name") {
      const card = event.target.closest("[data-row-id]");
      const item = draft.added.concat(draft.rows).find(candidate => candidate.id === card?.dataset.rowId);
      if (item) {
        card.querySelector("[data-locate]").textContent = `第 ${item.page + 1} 页 · ${item.raw_name || "新补录项目"}`;
        card.querySelector("[data-source-detail]").textContent = sourceDetail(item, card.hasAttribute("data-new-row"));
      }
    }
    markDirty();
  });
  $("#review-form").addEventListener("change", event => {
    syncForm();
    if (event.target.name === "conflict-choice") {
      const crp = draft.rows.find(item => item.id === "crp");
      crp.raw_value = event.target.value;
      $('[data-row-id="crp"] [name="raw_value"]').value = event.target.value;
    }
    if (event.target.name === "source_page") {
      const card = event.target.closest("[data-row-id]");
      const item = draft.added.find(candidate => candidate.id === card?.dataset.rowId);
      if (item) {
        card.querySelector("[data-locate]").textContent = `第 ${item.page + 1} 页 · ${item.raw_name || "新补录项目"}`;
        card.querySelector("[data-source-detail]").textContent = sourceDetail(item, true);
      }
    }
    markDirty();
  });
  $("#review-form").addEventListener("submit", event => event.preventDefault());
  $("#add-result").addEventListener("click", () => {
    syncForm();
    const item = {id: `manual-${crypto.randomUUID()}`, page: currentPage, box: null,
      raw_name: "", raw_value: "", raw_unit: "", reference_range_raw: "", report_flag_raw: "",
      specimen: "", method_raw: "", standard_name: "", excluded: false, exclusion_reason: "", unreadable: false};
    draft.added.push(item);
    const card = createCard(item, true);
    $("#added-results").append(card);
    markDirty();
    card.querySelector('[name="raw_name"]').focus();
  });
  $("#save-report").addEventListener("click", () => saveCurrent(false));
  $("#confirm-report").addEventListener("click", () => saveCurrent(true));
  $("#confirm-next").addEventListener("click", () => {
    if (!saveCurrent(true)) return;
    if (currentIndex < reports.length - 1) goToReport(currentIndex + 1);
    else setFeedback(reports.every(report => saved[report.id]?.confirmed)
      ? "所有已列出的报告均已核对。可用“上一份”返回查看。"
      : "当前已是最后一份；其他待核对报告可用“上一份”返回。", "success");
  });
  $("#previous-report").addEventListener("click", () => navigate(() => goToReport(currentIndex - 1)));
  $("#next-report").addEventListener("click", () => navigate(() => goToReport(currentIndex + 1)));
  $("#empty-next").addEventListener("click", () => {
    scenario = "normal";
    currentIndex = 1;
    loadCurrent();
    render();
  });
  $("#demo-scenario").addEventListener("change", event => {
    const nextScenario = event.target.value;
    event.target.value = scenario;
    navigate(() => {
      scenario = nextScenario;
      if (scenario === "conflict") currentIndex = 0;
      loadCurrent();
      render();
    });
  });
  $("#reset-demo").addEventListener("click", () => navigate(() => {
    localStorage.removeItem(storageKey);
    for (const key of Object.keys(saved)) delete saved[key];
    currentIndex = 0;
    scenario = "normal";
    loadCurrent();
    render();
  }));
  $("#leave-stay").addEventListener("click", () => {
    navigationAfterLeave = null;
    $("#leave-dialog").close();
  });
  $("#leave-discard").addEventListener("click", () => {
    const action = navigationAfterLeave;
    navigationAfterLeave = null;
    $("#leave-dialog").close();
    action?.();
  });
  $("#leave-save").addEventListener("click", () => {
    if (!saveCurrent(false)) {
      $("#leave-dialog").close();
      return;
    }
    const action = navigationAfterLeave;
    navigationAfterLeave = null;
    $("#leave-dialog").close();
    action?.();
  });
  $("#source-toggle").addEventListener("click", () => {
    const content = $("#source-content");
    content.hidden = !content.hidden;
    $("#source-toggle").setAttribute("aria-expanded", String(!content.hidden));
    $("#source-toggle").textContent = content.hidden ? "展开原图" : "收起原图";
    if (!content.hidden) requestAnimationFrame(layoutViewer);
  });
  $("#return-to-edit").addEventListener("click", () => {
    returnTarget?.scrollIntoView({block: "center", behavior: "smooth"});
    returnTarget?.focus();
  });
  $("#zoom-in").addEventListener("click", () => {
    zoom = Math.min(2.5, zoom + .25);
    layoutViewer();
  });
  $("#zoom-out").addEventListener("click", () => {
    zoom = Math.max(.5, zoom - .25);
    layoutViewer();
  });
  $("#zoom-fit").addEventListener("click", () => {
    zoom = 1;
    angle = 0;
    layoutViewer();
  });
  $("#rotate-image").addEventListener("click", () => {
    angle = (angle + 90) % 360;
    layoutViewer();
  });
  $("#source-image").addEventListener("load", layoutViewer);
  window.addEventListener("resize", layoutViewer);
  window.addEventListener("beforeunload", event => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
  render();
})();
