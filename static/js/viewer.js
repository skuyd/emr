(function () {
  "use strict";

  const viewer = document.querySelector("[data-viewer]");
  if (!viewer) return;

  const stage = viewer.querySelector("[data-viewer-stage]");
  const pageElement = viewer.querySelector("[data-viewer-page]");
  const image = viewer.querySelector("[data-viewer-image]");
  const highlight = viewer.querySelector("[data-viewer-highlight]");
  const loading = viewer.querySelector("[data-viewer-loading]");
  const error = viewer.querySelector("[data-viewer-error]");
  const pageStatus = viewer.querySelector("[data-viewer-page-status]");
  const zoomStatus = viewer.querySelector("[data-viewer-zoom-status]");
  const previous = viewer.querySelector("[data-viewer-previous]");
  const next = viewer.querySelector("[data-viewer-next]");
  const toolbar = viewer.querySelector(".viewer-toolbar");
  const embeddedFrame = document.body.classList.contains("viewer-embed-body") ? window.frameElement : null;
  const count = Number(viewer.dataset.pageCount);
  const pageUrlTemplate = viewer.dataset.pageUrlTemplate;
  const thumbnailSheetUrl = viewer.dataset.thumbnailSheetUrl;
  const evidencePage = Number(viewer.dataset.highlightPage || 0);
  const evidenceRect = (viewer.dataset.highlightRect || "").split(",").map(Number);
  const state = {
    page: Number(viewer.dataset.initialPage),
    zoom: 1,
    rotation: 0,
    x: 0,
    y: 0,
    dragging: false,
    pointerX: 0,
    pointerY: 0,
  };

  function pageUrl(page) {
    return pageUrlTemplate.replace("{page}", String(page));
  }

  function applyTransform() {
    pageElement.style.transform = `translate(${state.x}px, ${state.y}px) scale(${state.zoom}) rotate(${state.rotation}deg)`;
    zoomStatus.textContent = `${Math.round(state.zoom * 100)}%`;
  }

  function applyHighlight() {
    const visible = state.page === evidencePage && evidenceRect.length === 4 && evidenceRect.every(Number.isFinite);
    highlight.hidden = !visible;
    if (!visible) return;
    highlight.style.left = `${image.offsetLeft + image.clientWidth * evidenceRect[0] / 100}px`;
    highlight.style.top = `${image.offsetTop + image.clientHeight * evidenceRect[1] / 100}px`;
    highlight.style.width = `${image.clientWidth * evidenceRect[2] / 100}px`;
    highlight.style.height = `${image.clientHeight * evidenceRect[3] / 100}px`;
  }

  function fitPage() {
    if (!image.naturalWidth || !image.naturalHeight) return;
    const rotated = state.rotation % 180 !== 0;
    const width = rotated ? image.naturalHeight : image.naturalWidth;
    const height = rotated ? image.naturalWidth : image.naturalHeight;
    const availableWidth = Math.max(1, stage.clientWidth - 32);

    if (document.fullscreenElement === viewer) {
      viewer.style.height = "100%";
    } else {
      const scale = Math.min(1, availableWidth / width);
      const pageHeight = Math.max(160, Math.ceil(height * scale + 32));
      const borderHeight = viewer.offsetHeight - viewer.clientHeight;
      const viewerHeight = `${toolbar.offsetHeight + pageHeight + borderHeight}px`;
      viewer.style.height = viewerHeight;
      if (embeddedFrame) embeddedFrame.style.height = viewerHeight;
    }

    const scale = Math.min(1, availableWidth / width, Math.max(1, stage.clientHeight - 32) / height);
    image.style.width = `${image.naturalWidth * scale}px`;
    image.style.height = `${image.naturalHeight * scale}px`;
    applyHighlight();
  }

  function resetTransform() {
    state.zoom = 1;
    state.rotation = 0;
    state.x = 0;
    state.y = 0;
    applyTransform();
    fitPage();
  }

  function updateControls() {
    previous.disabled = state.page <= 1;
    next.disabled = state.page >= count;
    pageStatus.textContent = `第 ${state.page} / ${count} 页`;
    viewer.querySelectorAll("[data-viewer-thumbnail]").forEach((button) => {
      if (Number(button.dataset.viewerThumbnail) === state.page) {
        button.setAttribute("aria-current", "page");
      } else {
        button.removeAttribute("aria-current");
      }
    });
  }

  function loadPage(page, force) {
    const target = Math.max(1, Math.min(count, Number(page) || 1));
    if (!force && target === state.page && image.complete && image.naturalWidth) return;
    state.page = target;
    resetTransform();
    loading.hidden = false;
    error.hidden = true;
    image.hidden = false;
    image.alt = `原件第 ${target} 页`;
    image.src = pageUrl(target);
    applyHighlight();
    updateControls();
  }

  function setZoom(value) {
    state.zoom = Math.max(0.5, Math.min(5, value));
    if (state.zoom === 1) {
      state.x = 0;
      state.y = 0;
    }
    applyTransform();
  }

  image.addEventListener("load", function () {
    loading.hidden = true;
    error.hidden = true;
    image.hidden = false;
    fitPage();
    loadThumbnailSheet();
  });

  let thumbnailSheetObjectUrl = "";
  let thumbnailSheetRequested = false;
  function loadThumbnailSheet() {
    if (thumbnailSheetRequested || count <= 1 || !thumbnailSheetUrl) return;
    thumbnailSheetRequested = true;
    fetch(thumbnailSheetUrl, { credentials: "same-origin", cache: "no-store", headers: { "X-Patient-ID": document.querySelector('meta[name="patient-id"]')?.content || "" } })
      .then((response) => {
        if (!response.ok) throw new Error("thumbnail_unavailable");
        return response.blob();
      })
      .then((blob) => {
        thumbnailSheetObjectUrl = URL.createObjectURL(blob);
        viewer.querySelectorAll("[data-viewer-thumbnail]").forEach((button) => {
          const page = Number(button.dataset.viewerThumbnail);
          const icon = button.querySelector(".viewer-page-icon");
          icon.style.backgroundImage = `url(${thumbnailSheetObjectUrl})`;
          icon.style.backgroundSize = `100% ${count * 100}%`;
          icon.style.backgroundPosition = `center ${count === 1 ? 0 : (page - 1) * 100 / (count - 1)}%`;
          icon.dataset.loaded = "true";
        });
      })
      .catch(() => {});
  }
  image.addEventListener("error", function () {
    loading.hidden = true;
    image.hidden = true;
    error.hidden = false;
  });

  previous.addEventListener("click", () => loadPage(state.page - 1));
  next.addEventListener("click", () => loadPage(state.page + 1));
  viewer.querySelector("[data-viewer-zoom-in]").addEventListener("click", () => setZoom(state.zoom + 0.25));
  viewer.querySelector("[data-viewer-zoom-out]").addEventListener("click", () => setZoom(state.zoom - 0.25));
  viewer.querySelector("[data-viewer-fit]").addEventListener("click", resetTransform);
  viewer.querySelector("[data-viewer-rotate]").addEventListener("click", function () {
    state.rotation = (state.rotation + 90) % 360;
    state.x = 0;
    state.y = 0;
    applyTransform();
    fitPage();
  });
  viewer.querySelector("[data-viewer-retry]").addEventListener("click", () => loadPage(state.page, true));
  viewer.querySelectorAll("[data-viewer-thumbnail]").forEach((button) => {
    button.addEventListener("click", () => loadPage(Number(button.dataset.viewerThumbnail)));
  });
  viewer.querySelector("[data-viewer-fullscreen]").addEventListener("click", function () {
    if (viewer.requestFullscreen) viewer.requestFullscreen();
  });

  stage.addEventListener("wheel", function (event) {
    event.preventDefault();
    setZoom(state.zoom + (event.deltaY < 0 ? 0.15 : -0.15));
  }, { passive: false });
  stage.addEventListener("pointerdown", function (event) {
    state.dragging = true;
    state.pointerX = event.clientX;
    state.pointerY = event.clientY;
    stage.dataset.dragging = "true";
    stage.setPointerCapture(event.pointerId);
  });
  stage.addEventListener("pointermove", function (event) {
    if (!state.dragging) return;
    state.x += event.clientX - state.pointerX;
    state.y += event.clientY - state.pointerY;
    state.pointerX = event.clientX;
    state.pointerY = event.clientY;
    applyTransform();
  });
  function stopDragging() {
    state.dragging = false;
    stage.dataset.dragging = "false";
  }
  stage.addEventListener("pointerup", stopDragging);
  stage.addEventListener("pointercancel", stopDragging);

  document.addEventListener("keydown", function (event) {
    if (event.key === "ArrowLeft" || event.key === "PageUp") loadPage(state.page - 1);
    if (event.key === "ArrowRight" || event.key === "PageDown") loadPage(state.page + 1);
    if (event.key === "+" || event.key === "=") setZoom(state.zoom + 0.25);
    if (event.key === "-") setZoom(state.zoom - 0.25);
  });
  window.addEventListener("resize", fitPage);
  document.addEventListener("fullscreenchange", fitPage);
  window.addEventListener("beforeunload", function () {
    if (thumbnailSheetObjectUrl) URL.revokeObjectURL(thumbnailSheetObjectUrl);
  });

  const back = document.querySelector("[data-viewer-back]");
  if (back) {
    back.addEventListener("click", function (event) {
      if (window.history.length > 1 && document.referrer.startsWith(window.location.origin)) {
        event.preventDefault();
        window.history.back();
      }
    });
  }

  applyTransform();
  fitPage();
  updateControls();
  if (image.complete && image.naturalWidth) {
    loading.hidden = true;
    loadThumbnailSheet();
  }
}());
