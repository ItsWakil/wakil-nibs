/* Swatch gallery: color picking on the gallery, crop box and live color preview
   on the upload page. Pages still work without it, minus the preview and crop. */
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const clamp01 = (v) => Math.min(1, Math.max(0, v));
  const toHex = (r, g, b) => "#" + [r, g, b].map((v) => Math.round(v).toString(16).padStart(2, "0")).join("").toUpperCase();

  /* ---------------- Gallery: custom color + pick from photo ---------------- */
  const gallery = $("[data-gallery-url]");
  if (gallery) {
    // Keep the current sort and filters, just swap the color.
    const go = (hex) => {
      const url = new URL(location.href);
      url.searchParams.set("color", hex.replace("#", ""));
      location.href = url.toString();
    };

    const colorInput = $("[data-color-input]");
    if (colorInput) colorInput.addEventListener("change", () => go(colorInput.value));

    const pick = $("[data-photo-pick]");
    const wrap = $("[data-pick-wrap]");
    const canvas = $("[data-pick-canvas]");
    if (pick && canvas) {
      const ctx = canvas.getContext("2d", { willReadFrequently: true });
      pick.addEventListener("change", () => {
        const file = pick.files && pick.files[0];
        if (!file) return;
        const img = new Image();
        img.onload = () => {
          const scale = Math.min(1, 900 / Math.max(img.width, img.height));
          canvas.width = Math.round(img.width * scale);
          canvas.height = Math.round(img.height * scale);
          ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
          URL.revokeObjectURL(img.src);
          wrap.classList.add("is-on");
          canvas.scrollIntoView({ behavior: "smooth", block: "nearest" });
        };
        img.src = URL.createObjectURL(file);
      });
      canvas.addEventListener("pointerup", (e) => {
        const r = canvas.getBoundingClientRect();
        const x = Math.round(((e.clientX - r.left) / r.width) * canvas.width);
        const y = Math.round(((e.clientY - r.top) / r.height) * canvas.height);
        const size = 7; // average a small patch so paper fibres and noise don't win
        const sx = Math.max(0, x - 3), sy = Math.max(0, y - 3);
        const d = ctx.getImageData(sx, sy, size, size).data;
        let rr = 0, gg = 0, bb = 0, n = 0;
        for (let i = 0; i < d.length; i += 4) { rr += d[i]; gg += d[i + 1]; bb += d[i + 2]; n++; }
        if (n) go(toHex(rr / n, gg / n, bb / n));
      });
    }
  }

  /* ---------------- Upload: crop box + live preview ---------------- */
  const form = $("[data-upload]");
  if (!form) return;

  const photo = $("[data-photo]", form);
  const drop = $("[data-drop]", form);
  const stage = $("[data-stage]", form);
  const stageImg = $("[data-stage-img]", form);
  const crop = $("[data-crop]", form);
  const hint = $("[data-crop-hint]", form);
  const rechoose = $("[data-rechoose]", form);
  const status = $("[data-status]", form);
  const detectedWrap = $("[data-detected-wrap]", form);
  const detected = $("[data-detected]", form);
  const suggestWrap = $("[data-suggest-wrap]", form);
  const suggest = $("[data-suggest]", form);
  const inkSelect = $("[data-ink-select]", form);
  const submit = $("[data-submit]", form);
  const fields = { cx: $("[data-cx]", form), cy: $("[data-cy]", form), cw: $("[data-cw]", form), ch: $("[data-ch]", form) };

  let controller = null;
  let objectUrl = null;

  const setStatus = (msg, kind) => {
    status.textContent = msg || "";
    status.className = "sw-status" + (kind ? " is-" + kind : "");
  };

  const setCrop = (box) => {
    if (!box) {
      Object.values(fields).forEach((f) => (f.value = ""));
      crop.classList.remove("is-on");
      return;
    }
    fields.cx.value = box.x.toFixed(4); fields.cy.value = box.y.toFixed(4);
    fields.cw.value = box.w.toFixed(4); fields.ch.value = box.h.toFixed(4);
    Object.assign(crop.style, { left: box.x * 100 + "%", top: box.y * 100 + "%", width: box.w * 100 + "%", height: box.h * 100 + "%" });
    crop.classList.add("is-on");
  };

  async function runPreview() {
    const file = photo.files && photo.files[0];
    if (!file) return;
    if (controller) controller.abort();
    controller = new AbortController();
    const fd = new FormData();
    fd.append("photo", file);
    fd.append("csrf_token", $("[data-csrf]", form).value);  // app.py checks every POST
    Object.entries(fields).forEach(([k, f]) => fd.append(k, f.value));
    setStatus("Reading colors…");
    try {
      const res = await fetch(form.dataset.previewUrl, { method: "POST", body: fd, signal: controller.signal, credentials: "same-origin" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.error || (res.status === 413 ? "That photo is too large."
          : "Couldn't read that photo. Try reloading the page."));
      }
      detected.replaceChildren(...data.colors.map((c) => {
        const cell = document.createElement("div");
        const sw = document.createElement("i"); sw.style.background = c.hex;
        const hex = document.createElement("span"); hex.textContent = c.hex;
        const share = document.createElement("span"); share.textContent = `${Math.round(c.share * 100)}% · ${c.role}`;
        cell.append(sw, hex, share);
        return cell;
      }));
      detectedWrap.hidden = false;
      suggest.replaceChildren(...(data.suggestions || []).filter((s) => s.delta_e < 12).map((s) => {
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = `${s.brand} · ${s.name} (ΔE ${s.delta_e})`;
        b.addEventListener("click", () => { inkSelect.value = String(s.ink_id); inkSelect.focus(); });
        return b;
      }));
      suggestWrap.hidden = suggest.children.length === 0;
      setStatus(`Found ${data.colors.length} colors · shading: ${data.shading.toLowerCase()}`, "ok");
    } catch (err) {
      if (err.name === "AbortError") return;
      detectedWrap.hidden = true;
      setStatus(err.message, "err");
    }
  }

  photo.addEventListener("change", () => {
    const file = photo.files && photo.files[0];
    if (!file) return;
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = URL.createObjectURL(file);
    stageImg.src = objectUrl;
    stage.classList.add("is-on");
    drop.classList.add("has-photo");
    hint.hidden = false;
    rechoose.hidden = false;
    setCrop(null);
    runPreview();
  });

  rechoose.addEventListener("click", () => photo.click());

  // Drag a rectangle over the photo to crop.
  let start = null;
  const fracPoint = (e) => {
    const r = stage.getBoundingClientRect();
    return { x: clamp01((e.clientX - r.left) / r.width), y: clamp01((e.clientY - r.top) / r.height) };
  };
  stage.addEventListener("pointerdown", (e) => {
    start = fracPoint(e);
    stage.setPointerCapture(e.pointerId);
  });
  stage.addEventListener("pointermove", (e) => {
    if (!start) return;
    const p = fracPoint(e);
    setCrop({ x: Math.min(start.x, p.x), y: Math.min(start.y, p.y), w: Math.abs(p.x - start.x), h: Math.abs(p.y - start.y) });
  });
  stage.addEventListener("pointerup", (e) => {
    if (!start) return;
    const p = fracPoint(e);
    const box = { x: Math.min(start.x, p.x), y: Math.min(start.y, p.y), w: Math.abs(p.x - start.x), h: Math.abs(p.y - start.y) };
    start = null;
    setCrop(box.w > 0.03 && box.h > 0.03 ? box : null); // a tap clears the crop
    runPreview();
  });

  form.addEventListener("submit", () => {
    submit.disabled = true;
    submit.textContent = "Saving…";
  });
})();
