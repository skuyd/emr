(() => {
  "use strict";
  const root = document.querySelector("[data-lab-report-workspace]");
  const form = root?.querySelector("[data-report-form]");
  if (!form) return;

  const page = form.querySelector("[data-report-page]");
  const image = form.querySelector("[data-report-image]");
  const imageError = form.querySelector("[data-report-image-error]");
  const position = form.querySelector("[data-report-image-position]");
  const stage = form.querySelector("[data-report-image-stage]");
  const viewport = form.querySelector("[data-report-image-scroll]");
  const highlight = form.querySelector("[data-report-image-highlight]");
  const locationLabel = form.querySelector("[data-report-location]");
  const error = form.querySelector("[data-report-error]");
  const status = form.querySelector("[data-report-status]");
  const additionList = form.querySelector("[data-report-additions]");
  const additionTemplate = form.querySelector("[data-report-add-template]");
  const imageContent = form.querySelector("[data-report-image-content]");
  const imageToggle = form.querySelector("[data-report-toggle-image]");
  const leaveDialog = root.querySelector("[data-report-leave-dialog]");
  const returnButton = form.querySelector("[data-report-return-to-edit]");
  const sourceTabs = form.querySelectorAll("[data-report-source-page]");
  const rows = [...form.querySelectorAll("[data-report-observation]")];
  const counts = form.querySelector("[data-report-counts]");
  const reviewState = root.querySelector("[data-report-review-state]");
  const originalRowStates = new Map(rows.map(row => [row, row.querySelector("[data-report-row-state]").textContent]));
  let returnTarget = null;
  let dirty = false;
  let pending = false;
  let zoom = 1;
  let angle = 0;
  image.addEventListener("error", () => { imageError.hidden = false; });
  image.addEventListener("load", () => { imageError.hidden = true; transform(); });
  if (image.complete && !image.naturalWidth) imageError.hidden = false;

  function showPage(index, polygon, reveal = true) {
    if (reveal) {
      imageContent.hidden = false;
      imageToggle.setAttribute("aria-expanded", "true");
      imageToggle.textContent = "收起原图";
    }
    page.value = String(index);
    const selected = page.selectedOptions[0];
    if (!selected) return;
    if (image.getAttribute("src") !== selected.dataset.imageUrl) image.src = selected.dataset.imageUrl;
    image.alt = selected.dataset.pageLabel;
    form.querySelector("[data-report-page-label]").textContent = selected.dataset.pageLabel;
    for (const tab of sourceTabs) tab.setAttribute("aria-pressed", String(tab.dataset.reportSourcePage === page.value));
    const region = selected.dataset.region ? JSON.parse(selected.dataset.region) : null;
    const marked = polygon?.length ? polygon : region;
    highlight.hidden = true;
    if (marked?.length) {
      const xs = marked.map(point => Number(point[0]));
      const ys = marked.map(point => Number(point[1]));
      highlight.style.left = `${Math.min(...xs) * 100}%`;
      highlight.style.top = `${Math.min(...ys) * 100}%`;
      highlight.style.width = `${(Math.max(...xs) - Math.min(...xs)) * 100}%`;
      highlight.style.height = `${(Math.max(...ys) - Math.min(...ys)) * 100}%`;
      highlight.hidden = false;
      locationLabel.textContent = polygon?.length ? "已定位原件中的指标区域。" : "已定位本报告来源区域；指标没有可靠的精确坐标。";
    } else {
      locationLabel.textContent = "已定位到对应原页；此指标没有可靠的精确坐标。";
    }
    transform();
  }

  function transform() {
    if (!image.naturalWidth || !viewport.clientWidth) return;
    const style = getComputedStyle(viewport);
    const width = viewport.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const height = width * image.naturalHeight / image.naturalWidth;
    const turned = angle % 180 !== 0;
    position.style.width = `${width}px`;
    stage.style.width = `${Math.max(width, zoom * (turned ? height : width))}px`;
    stage.style.height = `${zoom * (turned ? width : height)}px`;
    position.style.transform = `translate(-50%, -50%) rotate(${angle}deg) scale(${zoom})`;
    viewport.scrollLeft = 0;
    viewport.scrollTop = 0;
    if (!highlight.hidden) {
      const source = viewport.getBoundingClientRect();
      const target = highlight.getBoundingClientRect();
      viewport.scrollLeft += target.left + target.width / 2 - (source.left + viewport.clientWidth / 2);
      viewport.scrollTop += target.top + target.height / 2 - (source.top + viewport.clientHeight / 2);
    }
  }
  new ResizeObserver(transform).observe(viewport);
  for (const tab of sourceTabs) tab.addEventListener("click", () => {
    for (const row of rows) row.classList.remove("is-active", "labs-report-focused");
    showPage(tab.dataset.reportSourcePage);
  });
  page.addEventListener("change", () => showPage(page.value));
  imageToggle.addEventListener("click", () => {
    imageContent.hidden = !imageContent.hidden;
    imageToggle.setAttribute("aria-expanded", String(!imageContent.hidden));
    imageToggle.textContent = imageContent.hidden ? "展开原图" : "收起原图";
    if (!imageContent.hidden) transform();
  });
  for (const control of form.querySelectorAll("[data-report-zoom]")) {
    control.addEventListener("click", () => {
      zoom = control.dataset.reportZoom === "fit" ? 1 :
        Math.max(.5, Math.min(3, zoom + (control.dataset.reportZoom === "in" ? .25 : -.25)));
      if (control.dataset.reportZoom === "fit") angle = 0;
      transform();
    });
  }
  form.querySelector("[data-report-rotate]").addEventListener("click", () => {
    angle = (angle + 90) % 360;
    transform();
  });
  function locateRow(row, reveal) {
    for (const item of rows) item.classList.toggle("is-active", item === row);
    showPage(row.dataset.sourceIndex, row.dataset.polygon ? JSON.parse(row.dataset.polygon) : null, reveal);
  }
  form.addEventListener("focusin", event => {
    if (!event.target.matches("input:not([type=hidden]), select")) return;
    const row = event.target.closest("[data-report-observation]");
    if (row) {
      returnTarget = event.target;
      locateRow(row, false);
    }
    const fieldBox = event.target.getBoundingClientRect();
    const headerBottom = document.querySelector(".app-header").getBoundingClientRect().bottom;
    const dockTop = form.querySelector(".labs-report-actions")?.getBoundingClientRect().top || innerHeight;
    if (fieldBox.top < headerBottom || fieldBox.bottom > dockTop) event.target.scrollIntoView({block: "center"});
  });
  for (const control of form.querySelectorAll("[data-report-locate]")) {
    control.addEventListener("click", () => {
      const row = control.closest("[data-report-observation]");
      if (!returnTarget || !row.contains(returnTarget)) returnTarget = control;
      locateRow(row, true);
      if (matchMedia("(max-width: 928px)").matches) {
        returnButton.hidden = false;
        form.querySelector(".labs-report-original").scrollIntoView({block: "start"});
      }
    });
  }
  function revealField(field) {
    for (let parent = field.parentElement; parent && parent !== form; parent = parent.parentElement) {
      if (parent.tagName === "DETAILS") parent.open = true;
    }
    field.scrollIntoView({block: "center"});
    field.focus({preventScroll: true});
  }
  returnButton.addEventListener("click", () => { if (returnTarget) revealField(returnTarget); });
  const focused = form.querySelector(".labs-report-focused");
  if (focused) {
    focused.scrollIntoView({block: "center"});
    locateRow(focused, false);
  }
  if (!error) return;

  new ResizeObserver(entries => {
    root.style.setProperty("--report-dock-height", `${entries[0].target.offsetHeight}px`);
  }).observe(form.querySelector(".labs-report-actions"));

  function updateCounts() {
    const excluded = rows.filter(row => row.querySelector("[name=decision]").value === "EXCLUDE").length;
    const retained = rows.filter(row => row.querySelector("[name=decision]").value !== "EXCLUDE"
      && row.dataset.manualConflict !== "true").length + additionList.children.length;
    counts.textContent = `保留 ${retained} 项 · 排除 ${excluded} 项 · ${counts.dataset.sourceCount} 处来源`;
    form.querySelector("[data-report-row-count]").textContent = `（${rows.length + additionList.children.length}）`;
  }

  function markDirty(row) {
    dirty = true;
    status.textContent = "有未保存的修改；确认报告时会一并保存。";
    status.hidden = false;
    reviewState.textContent = root.dataset.confirmed === "true" ? "待重新确认" : "待核对 · 未保存";
    reviewState.dataset.reportReviewState = "dirty";
    if (row) row.querySelector("[data-report-row-state]").textContent = row.classList.contains("is-excluded")
      ? "已排除" : row.dataset.manualConflict === "true" ? originalRowStates.get(row) : "已修改";
    updateCounts();
  }

  for (const row of rows) row.querySelector("[data-report-exclude]").addEventListener("click", () => {
    const decision = row.querySelector("[name=decision]");
    const excluding = decision.value !== "EXCLUDE";
    decision.value = excluding ? "EXCLUDE" : row.dataset.excluded === "true" ? "RESTORE" : "";
    row.classList.toggle("is-excluded", excluding);
    row.querySelector("[data-report-exclude]").textContent = excluding ? "恢复此项" : "排除此项";
    row.querySelector("[data-report-reason]").hidden = !excluding;
    const reason = row.querySelector("[name=reason]");
    reason.disabled = !excluding || row.dataset.excluded === "true";
    reason.required = excluding;
    if (excluding) {
      row.querySelector(".labs-report-more").open = true;
      if (!reason.disabled) reason.focus();
    }
    markDirty(row);
  });

  form.querySelector("[data-report-add]")?.addEventListener("click", () => {
    additionList.append(additionTemplate.content.cloneNode(true));
    additionList.lastElementChild.querySelector("[name=unit_id]").selectedIndex = Number(page.value);
    additionList.lastElementChild.querySelector("[name=raw_name]").focus();
    markDirty();
  });
  additionList.addEventListener("click", event => {
    if (!event.target.matches("[data-report-remove-addition]")) return;
    event.target.closest("[data-report-addition]").remove();
    markDirty();
  });
  form.addEventListener("input", event => {
    if (event.target.matches("[name=sampled_at]")) event.target.setCustomValidity("");
    markDirty(event.target.closest("[data-report-observation]"));
  });
  form.addEventListener("change", event => {
    if (event.target !== page) markDirty(event.target.closest("[data-report-observation]"));
  });
  window.addEventListener("beforeunload", event => {
    if (!dirty || pending) return;
    event.preventDefault();
    event.returnValue = "";
  });

  function collect() {
    const edits = {observations: [], reports: [], report_resolutions: [], additions: [], resolutions: []};
    for (const unit of form.querySelectorAll("[data-report-unit]")) {
      const changes = {};
      for (const input of unit.querySelectorAll("input[name]")) {
        if (input.value.trim() !== input.defaultValue) changes[input.name] = input.value.trim();
      }
      if (Object.keys(changes).length) edits.reports.push({unit_id: unit.dataset.reportUnit,
        expected_revision: Number(unit.dataset.revision), changes});
      const resolution = unit.querySelector("[name=report_resolution]")?.value;
      if (resolution) edits.report_resolutions.push({unit_id: unit.dataset.reportUnit,
        expected_revision: Number(unit.dataset.revision), decision: resolution});
    }
    for (const row of form.querySelectorAll("[data-report-observation]")) {
      const changes = {};
      for (const input of row.querySelectorAll(".labs-report-fields input[name], .labs-report-fields select[name]")) {
        const original = input.tagName === "SELECT" ? (input.querySelector("option[selected]")?.value || "") : input.defaultValue;
        if (input.value.trim() !== original) changes[input.name] = input.value.trim();
      }
      const decision = row.querySelector("[name=decision]")?.value || "";
      const excluded = row.dataset.excluded === "true";
      const reason = row.querySelector("[name=reason]")?.value.trim() || "";
      const base = {id: row.dataset.reportObservation, expected_revision: Number(row.dataset.revision)};
      if (decision === "EXCLUDE" && !excluded)
        edits.observations.push({...base, action: "EXCLUDE", reason, ...(Object.keys(changes).length ? {changes} : {})});
      else if (decision === "EXCLUDE" && reason !== row.dataset.exclusionReason)
        throw new Error("请先恢复此项，再重新排除以更改原因。");
      else if (decision === "RESTORE" && excluded)
        edits.observations.push({...base, action: "RESTORE", ...(Object.keys(changes).length ? {changes} : {})});
      else if (Object.keys(changes).length) edits.observations.push({...base, changes});
    }
    for (const addition of additionList.querySelectorAll("[data-report-addition]")) {
      const values = {};
      for (const field of addition.querySelectorAll("[name]")) values[field.name] = field.value.trim();
      edits.additions.push(values);
    }
    for (const group of form.querySelectorAll("[data-report-conflict]")) {
      const selected = group.querySelector("input:checked");
      if (selected) edits.resolutions.push({manual_identity: group.dataset.reportConflict, winner_id: selected.value});
    }
    return edits;
  }

  function validateDraft() {
    for (const input of form.querySelectorAll("[name=sampled_at]")) {
      const value = input.value.trim();
      input.setCustomValidity(value && !/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?$/.test(value)
        ? "采样时间请按原件填写完整日期和时分，或留空。" : "");
    }
    for (const row of form.querySelectorAll("[data-report-observation]")) {
      const excluding = row.querySelector("[name=decision]")?.value === "EXCLUDE";
      const reason = row.querySelector("[name=reason]");
      if (reason) reason.required = excluding;
    }
    const invalid = [...form.elements].find(field => field.willValidate && !field.validity.valid);
    if (invalid) revealField(invalid);
    return form.reportValidity();
  }

  async function submit(intent, destination) {
    if (pending) return;
    error.hidden = true;
    status.hidden = true;
    if (!validateDraft()) return false;
    let edits;
    try { edits = collect(); }
    catch (problem) { error.textContent = problem.message; error.hidden = false; error.focus(); return false; }
    const payload = new FormData(form);
    payload.set("report_key", form.dataset.reportKey);
    payload.set("token", form.dataset.token);
    payload.set("operation_id", form.dataset.operationId);
    payload.set("intent", intent);
    payload.set("edits", JSON.stringify(edits));
    pending = true;
    const submitButtons = form.querySelectorAll("button[type=submit]");
    for (const button of submitButtons) button.disabled = true;
    status.textContent = intent === "save" ? "正在保存修改…" : "正在保存并确认本报告…";
    status.hidden = false;
    try {
      const response = await fetch(form.action || location.href, {method: "POST", body: payload,
        credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "提交失败，请检查后重试。");
      dirty = false;
      status.textContent = intent === "save" ? "修改已保存。" : "本报告已确认。";
      status.hidden = false;
      window.location.assign(destination || result.next_url);
      return true;
    } catch (problem) {
      status.hidden = true;
      error.textContent = problem.message;
      error.hidden = false;
      error.setAttribute("tabindex", "-1");
      error.focus();
      return false;
    } finally {
      pending = false;
      for (const button of submitButtons) button.disabled = false;
    }
  }
  form.addEventListener("submit", event => {
    event.preventDefault();
    submit(event.submitter?.value || "save");
  });
  for (const link of root.querySelectorAll("[data-report-navigation]")) {
    link.addEventListener("click", event => {
      if (!dirty || !leaveDialog) return;
      event.preventDefault();
      leaveDialog.dataset.destination = link.href;
      leaveDialog.showModal();
    });
  }
  leaveDialog?.querySelector("[data-report-leave-save]").addEventListener("click", async () => {
    const destination = leaveDialog.dataset.destination;
    leaveDialog.close();
    await submit("save", destination);
  });
  leaveDialog?.querySelector("[data-report-leave-discard]").addEventListener("click", () => {
    dirty = false;
    window.location.assign(leaveDialog.dataset.destination);
  });
  leaveDialog?.querySelector("[data-report-leave-stay]").addEventListener("click", () => leaveDialog.close());
})();
