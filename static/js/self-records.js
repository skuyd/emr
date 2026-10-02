(() => {
  "use strict";

  const pad = (value) => String(value).padStart(2, "0");
  const deviceDate = () => {
    const now = new Date();
    return [now.getFullYear(), pad(now.getMonth() + 1), pad(now.getDate())].join("-");
  };
  const deviceMinute = () => {
    const now = new Date();
    return [now.getFullYear(), pad(now.getMonth() + 1), pad(now.getDate())].join("-") +
      "T" + pad(now.getHours()) + ":" + pad(now.getMinutes());
  };
  const element = (tag, className, value) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = value;
    return node;
  };

  const browse = document.querySelector("[data-record-browse]");
  if (browse) {
    const params = new URLSearchParams(location.search);
    const today = deviceDate();
    if (params.get("today") !== today) {
      if (!params.has("month")) params.set("month", today.slice(0, 7));
      if (!params.has("date")) params.set("date", today);
      params.set("today", today);
      location.replace(location.pathname + "?" + params.toString());
      return;
    }
    const todayLink = browse.querySelector("[data-record-today]");
    todayLink.addEventListener("click", (event) => {
      event.preventDefault();
      const url = new URL(todayLink.href);
      const current = deviceDate();
      url.searchParams.set("month", current.slice(0, 7));
      url.searchParams.set("date", current);
      url.searchParams.set("today", current);
      location.assign(url.href);
    });
    browse.querySelector("#record-kind-filter").addEventListener("change", (event) => event.target.form.submit());
  }

  const entry = document.querySelector("[data-record-entry]");
  if (!entry) return;

  const form = entry.querySelector("[data-record-form]");
  const kindInput = form.elements.kind;
  const timeInput = form.elements.measured_local;
  const dateInput = form.elements.record_date;
  const valueInput = form.elements.value;
  const unitInput = form.elements.unit;
  const symptomInput = form.elements.symptom_name;
  const severityInput = form.elements.severity;
  const revisionInput = form.elements.expected_revision;
  const keyInput = form.elements.creation_key;
  const kindButtons = [...entry.querySelectorAll("[data-kind-button]")];
  const notice = entry.querySelector("[data-form-notice]");
  const errorBox = entry.querySelector("[data-form-error]");
  const sideContent = entry.querySelector("[data-existing-content]");
  const sideDate = entry.querySelector("[data-existing-date]");
  const sideCount = entry.querySelector("[data-existing-count]");
  const sideKind = entry.querySelector("[data-existing-kind]");
  const cancelEdit = entry.querySelector("[data-cancel-edit]");
  const saveButton = entry.querySelector("[data-save-button]");
  const returnLink = entry.querySelector("[data-return-list]");
  const returnButton = entry.querySelector("[data-return-button]");
  const labels = {WEIGHT: "体重", TEMPERATURE: "体温", SYMPTOM: "症状", ECOG: "ECOG评分"};
  const units = {WEIGHT: ["kg", "g", "lb"], TEMPERATURE: ["°C", "°F"]};
  const nowTime = deviceMinute();
  const nowDate = deviceDate();
  if (entry.dataset.editId === "" && entry.dataset.bound === "false") {
    timeInput.value = nowTime;
    dateInput.value = nowDate;
  }
  let kind = entry.dataset.kind;
  let editing = entry.dataset.editId || "";
  let editUrl = editing ? form.action : "";
  let beforeEdit = null;
  let records = [];
  let savedId = "";
  let lastSaved = null;
  let readSequence = 0;
  let activeRead = null;
  let busy = false;
  let drafts = {
    WEIGHT: {time: nowTime, value: "", unit: "kg"},
    TEMPERATURE: {time: nowTime, value: "", unit: "°C"},
    SYMPTOM: {time: nowTime, name: "", severity: ""},
    ECOG: {date: nowDate, score: ""}
  };

  const checkedScore = () => form.querySelector('input[name="score"]:checked')?.value || "";
  const remember = () => {
    if (kind === "ECOG") drafts.ECOG = {date: dateInput.value, score: checkedScore()};
    else if (kind === "SYMPTOM") drafts.SYMPTOM = {
      time: timeInput.value, name: symptomInput.value, severity: severityInput.value
    };
    else drafts[kind] = {time: timeInput.value, value: valueInput.value, unit: unitInput.value};
  };
  if (kind === "ECOG") drafts.ECOG = {date: dateInput.value, score: checkedScore()};
  else if (kind === "SYMPTOM") drafts.SYMPTOM = {
    time: timeInput.value, name: symptomInput.value, severity: severityInput.value
  };
  else drafts[kind] = {time: timeInput.value, value: valueInput.value,
                       unit: unitInput.value || units[kind][0]};

  const showError = (message) => {
    errorBox.textContent = message;
    errorBox.hidden = false;
  };
  const clearError = () => {
    errorBox.textContent = "";
    errorBox.hidden = true;
    form.querySelectorAll("[aria-invalid]").forEach((field) => field.removeAttribute("aria-invalid"));
  };
  const errorMessage = (body, fallback) => {
    const fields = Object.entries(body?.errors || {});
    fields.forEach(([name]) => {
      const field = form.elements[name];
      if (field?.setAttribute) field.setAttribute("aria-invalid", "true");
    });
    const messages = fields.flatMap(([, errors]) => errors.map((error) => error.message));
    return messages.join("；") || body?.error || fallback;
  };
  const setBusy = (value) => {
    busy = value;
    saveButton.disabled = value;
    cancelEdit.disabled = value;
    kindButtons.forEach((button) => { button.disabled = value || Boolean(editing) && button.dataset.kindButton !== kind; });
    [timeInput, dateInput, valueInput, unitInput, symptomInput, severityInput].forEach((field) => {
      if (value) field.disabled = true;
    });
    form.querySelectorAll('input[name="score"]').forEach((field) => { if (value) field.disabled = true; });
    if (!value) render();
  };
  const currentDate = () => kind === "ECOG" ? dateInput.value : timeInput.value.slice(0, 10);
  const updateReturn = () => {
    const url = new URL(returnLink.href);
    url.searchParams.set("kind", "");
    url.searchParams.set("view", entry.dataset.view === "list" ? "list" : "calendar");
    if (lastSaved) {
      url.searchParams.set("month", lastSaved.date.slice(0, 7));
      url.searchParams.set("date", lastSaved.date);
    }
    returnLink.href = url.href;
  };
  const render = () => {
    kindInput.value = kind;
    entry.querySelector("[data-entry-title]").textContent = editing ? "更正" + labels[kind] + "记录" : "记一条";
    entry.querySelector("[data-entry-subtitle]").textContent = editing
      ? "保存后直接更新这条记录。" : "记录此刻，也可以补记之前的情况。";
    saveButton.textContent = editing ? "保存更正" : "保存记录";
    cancelEdit.hidden = !editing;
    returnButton.hidden = Boolean(editing);
    form.action = editing ? editUrl : entry.dataset.createUrl;
    kindButtons.forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.kindButton === kind));
      button.disabled = busy || Boolean(editing) && button.dataset.kindButton !== kind;
    });
    const ecog = kind === "ECOG";
    const symptom = kind === "SYMPTOM";
    entry.querySelector("[data-time-field]").hidden = ecog;
    entry.querySelector("[data-date-field]").hidden = !ecog;
    entry.querySelector("[data-quantity-field]").hidden = symptom || ecog;
    entry.querySelector("[data-symptom-fields]").hidden = !symptom;
    const ecogFields = entry.querySelector("[data-ecog-fields]");
    ecogFields.hidden = !ecog;
    ecogFields.disabled = !ecog || busy;
    ecogFields.querySelectorAll('input[name="score"]').forEach((radio) => { radio.disabled = !ecog || busy; });
    timeInput.disabled = ecog || busy;
    dateInput.disabled = !ecog || busy;
    valueInput.disabled = symptom || ecog || busy;
    unitInput.disabled = symptom || ecog || busy;
    symptomInput.disabled = !symptom || busy;
    severityInput.disabled = !symptom || busy;
    entry.querySelector("[data-time-label]").textContent = symptom ? "发生时间" : "测量时间";
    sideKind.textContent = labels[kind];
    if (ecog) {
      dateInput.value = drafts.ECOG.date;
      form.querySelectorAll('input[name="score"]').forEach((radio) => {
        radio.checked = radio.value === drafts.ECOG.score;
      });
    } else if (symptom) {
      timeInput.value = drafts.SYMPTOM.time;
      symptomInput.value = drafts.SYMPTOM.name;
      severityInput.value = drafts.SYMPTOM.severity;
    } else {
      timeInput.value = drafts[kind].time;
      valueInput.value = drafts[kind].value;
      entry.querySelector("[data-value-label]").textContent = labels[kind];
      unitInput.replaceChildren(...units[kind].map((unit) => new Option(unit, unit)));
      unitInput.value = drafts[kind].unit;
    }
  };

  const refreshExisting = async () => {
    const day = currentDate();
    const requestedKind = kind;
    const sequence = ++readSequence;
    activeRead?.abort();
    sideDate.textContent = day ? day + (day === deviceDate() ? " · 今天" : "") : "";
    sideCount.textContent = "";
    sideContent.replaceChildren();
    if (!day || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(day)) {
      sideContent.textContent = "请先选择日期，查看当天已记录内容。";
      return;
    }
    sideContent.textContent = "正在读取已有记录…";
    activeRead = new AbortController();
    try {
      const url = new URL(entry.dataset.existingUrl, location.origin);
      url.searchParams.set("patient", entry.dataset.patient);
      url.searchParams.set("date", day);
      url.searchParams.set("kind", requestedKind);
      const response = await fetch(url, {credentials: "same-origin", headers: {Accept: "application/json"},
                                         signal: activeRead.signal});
      if (!response.ok) throw new Error("read failed");
      const body = await response.json();
      if (sequence !== readSequence || day !== currentDate() || requestedKind !== kind) return;
      records = body.records;
      sideCount.textContent = records.length + " 条";
      sideContent.replaceChildren();
      if (!records.length) {
        sideContent.textContent = "当天还没有" + labels[kind] + "记录";
        return;
      }
      const list = element("ul", "self-record-existing-list");
      records.forEach((record) => {
        const item = element("li", "self-record-existing-item");
        item.dataset.recordId = record.id;
        const line = element("p", "self-record-existing-line",
          (record.time ? record.time + " · " : "") + record.label);
        item.append(line);
        if (record.id === editing) item.append(element("span", "self-record-state", "正在更正"));
        else if (record.id === savedId) item.append(element("span", "self-record-state", "刚刚保存"));
        const actions = element("div", "self-record-row-actions");
        const edit = element("button", "self-record-row-action", "更正");
        edit.type = "button";
        edit.disabled = record.id === editing;
        edit.addEventListener("click", () => openEdit(record));
        const remove = element("button", "self-record-row-action self-record-delete-action", "删除");
        remove.type = "button";
        remove.addEventListener("click", () => confirmDelete(record, item, actions));
        actions.append(edit, remove);
        item.append(actions);
        list.append(item);
      });
      sideContent.append(list);
    } catch (error) {
      if (error.name === "AbortError" || sequence !== readSequence) return;
      records = [];
      sideCount.textContent = "";
      sideContent.replaceChildren(element("p", "visit-error", "读取失败，请重试。"));
      const retry = element("button", "button button--secondary", "重试");
      retry.type = "button";
      retry.addEventListener("click", refreshExisting);
      sideContent.append(retry);
    }
  };

  const createUrl = () => {
    const url = new URL(entry.dataset.createUrl, location.origin);
    url.searchParams.set("patient", entry.dataset.patient);
    url.searchParams.set("view", entry.dataset.view);
    return url.pathname + url.search;
  };
  const finishEdit = () => {
    if (beforeEdit) {
      kind = beforeEdit.kind;
      drafts = beforeEdit.drafts;
    } else {
      const draft = drafts[kind];
      if (kind === "ECOG") drafts.ECOG = {date: deviceDate(), score: ""};
      else if (kind === "SYMPTOM") drafts.SYMPTOM = {time: deviceMinute(), name: "", severity: ""};
      else drafts[kind] = {time: deviceMinute(), value: "", unit: draft.unit};
    }
    beforeEdit = null;
    editing = "";
    editUrl = "";
    revisionInput.value = "";
    history.replaceState(null, "", createUrl());
    clearError();
    render();
    refreshExisting();
  };
  const openEdit = (record) => {
    if (busy) return;
    remember();
    if (!editing) beforeEdit = {kind, drafts: JSON.parse(JSON.stringify(drafts))};
    editing = record.id;
    editUrl = record.edit_url;
    kind = record.kind;
    revisionInput.value = String(record.revision);
    if (kind === "ECOG") drafts.ECOG = {date: record.date, score: String(record.score)};
    else if (kind === "SYMPTOM") drafts.SYMPTOM = {
      time: record.date + "T" + record.time, name: record.symptom_name, severity: record.severity
    };
    else drafts[kind] = {time: record.date + "T" + record.time, value: record.value, unit: record.unit};
    notice.hidden = true;
    clearError();
    const url = new URL(editUrl, location.origin);
    url.searchParams.set("patient", entry.dataset.patient);
    url.searchParams.set("view", entry.dataset.view);
    history.replaceState(null, "", url.pathname + url.search);
    render();
    refreshExisting();
  };
  const confirmDelete = (record, item, actions) => {
    if (busy) return;
    actions.hidden = true;
    const group = element("div", "self-record-delete-confirm");
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "删除确认");
    group.append(element("p", "", "确认删除这条" + record.kind_label + "记录？"));
    group.append(element("p", "self-record-delete-summary",
      record.date + (record.time ? " " + record.time : "") + " · " + record.label));
    const cancel = element("button", "button button--secondary", "取消");
    cancel.type = "button";
    cancel.addEventListener("click", () => {
      group.remove();
      actions.hidden = false;
      actions.querySelector("button:last-child").focus();
    });
    const remove = element("button", "button self-record-danger", "确认删除");
    remove.type = "button";
    remove.addEventListener("click", async () => {
      if (busy) return;
      remove.disabled = true;
      const payload = new FormData();
      payload.set("csrfmiddlewaretoken", form.elements.csrfmiddlewaretoken.value);
      payload.set("patient_id", entry.dataset.patient);
      payload.set("expected_revision", String(record.revision));
      payload.set("confirm", "delete");
      payload.set("view", entry.dataset.view);
      try {
        const response = await fetch(record.delete_url, {
          method: "POST", body: payload, credentials: "same-origin", headers: {Accept: "application/json"}
        });
        const body = await response.json();
        if (!response.ok) throw new Error(errorMessage(body, "删除失败，请重试。"));
        if (editing === record.id) finishEdit();
        else refreshExisting();
        notice.textContent = "已删除记录。";
        notice.hidden = false;
        clearError();
      } catch (error) {
        showError(error.message || "删除失败，请重试。");
        remove.disabled = false;
      }
    });
    group.append(cancel, remove);
    item.append(group);
    cancel.focus();
  };

  kindButtons.forEach((button) => button.addEventListener("click", () => {
    if (editing || busy || button.dataset.kindButton === kind) return;
    remember();
    kind = button.dataset.kindButton;
    clearError();
    render();
    refreshExisting();
  }));
  [timeInput, dateInput].forEach((field) => field.addEventListener("input", () => {
    remember();
    refreshExisting();
  }));
  cancelEdit.addEventListener("click", finishEdit);
  returnButton.addEventListener("click", () => { location.assign(returnLink.href); });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    remember();
    clearError();
    notice.hidden = true;
    const submittedKind = kind;
    const wasEditing = Boolean(editing);
    const payload = new FormData(form);
    setBusy(true);
    try {
      const response = await fetch(form.action, {
        method: "POST", body: payload, credentials: "same-origin", headers: {Accept: "application/json"}
      });
      const body = await response.json();
      if (!response.ok) throw new Error(errorMessage(body, "保存失败，请重试。"));
      const record = body.record;
      lastSaved = record;
      savedId = record.id;
      if (wasEditing) {
        setBusy(false);
        finishEdit();
      } else {
        if (submittedKind === "ECOG") drafts.ECOG = {date: deviceDate(), score: ""};
        else if (submittedKind === "SYMPTOM") drafts.SYMPTOM = {
          time: deviceMinute(), name: "", severity: ""
        };
        else drafts[submittedKind] = {
          time: deviceMinute(), value: "", unit: drafts[submittedKind].unit
        };
        keyInput.value = body.next_creation_key;
        setBusy(false);
        refreshExisting();
      }
      updateReturn();
      notice.textContent = (wasEditing ? "已保存更正" : "已保存") + " · " +
        record.kind_label + " " + record.label + " · " + record.date +
        (record.time ? " " + record.time : "") + "。可继续添加下一条。";
      notice.hidden = false;
    } catch (error) {
      setBusy(false);
      showError(error.message || "保存失败，请重试。");
    }
  });

  render();
  refreshExisting();
})();
