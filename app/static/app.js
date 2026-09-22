/* DICOM anonymization workbench – single page controller. */
(() => {
  "use strict";

  const state = {
    files: [],
    jobId: null,
    profile: "standard",
    preview: null,
  };

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const els = {
    dropzone: $("#dropzone"),
    fileInput: $("#file-input"),
    fileInputFiles: $("#file-input-files"),
    pickFiles: $("#pick-files"),
    pickFolder: $("#pick-folder"),
    uploadList: $("#upload-list"),
    btnUpload: $("#btn-upload"),
    uploadStatus: $("#upload-status"),
    profileCards: $("#profile-cards"),
    summary: $("#summary-bar"),
    hierarchy: $("#hierarchy"),
    btnBack: $("#btn-back"),
    btnAnon: $("#btn-anonymize"),
    previewStatus: $("#preview-status"),
    tabChanges: $("#tab-changes"),
    tabUids: $("#tab-uids"),
    tabFiles: $("#tab-files"),
    doneSummary: $("#done-summary"),
    doneFailures: $("#done-failures"),
    linkZip: $("#link-zip"),
    linkReport: $("#link-report"),
    btnAgain: $("#btn-again"),
    btnRestart: $("#btn-restart"),
  };

  // ---------------------------------------------------------------- step UI
  function showStep(n) {
    $$(".panel").forEach((p, i) => p.classList.toggle("active", i === n - 1));
    $$(".step").forEach((s) => s.classList.toggle("active", Number(s.dataset.step) <= n));
  }

  // ------------------------------------------------------------- file picker
  els.pickFiles.addEventListener("click", (e) => { e.stopPropagation(); els.fileInputFiles.click(); });
  els.pickFolder.addEventListener("click", (e) => { e.stopPropagation(); els.fileInput.click(); });
  els.fileInput.addEventListener("change", () => addPicked(els.fileInput.files));
  els.fileInputFiles.addEventListener("change", () => addPicked(els.fileInputFiles.files));
  els.dropzone.addEventListener("click", () => els.fileInputFiles.click());

  ["dragenter", "dragover"].forEach((ev) =>
    els.dropzone.addEventListener(ev, (e) => { e.preventDefault(); els.dropzone.classList.add("dragover"); })
  );
  ["dragleave", "drop"].forEach((ev) =>
    els.dropzone.addEventListener(ev, (e) => { e.preventDefault(); els.dropzone.classList.remove("dragover"); })
  );
  els.dropzone.addEventListener("drop", (e) => {
    const items = e.dataTransfer.items;
    if (items && items.length && items[0].webkitGetAsEntry) {
      state.files = [];
      const entries = [];
      for (const it of items) {
        const entry = it.webkitGetAsEntry();
        if (entry) entries.push(entry);
      }
      Promise.all(entries.map((en) => walkEntry(en, ""))).then(renderFileList);
    } else {
      addPicked(e.dataTransfer.files);
    }
  });

  function walkEntry(entry, prefix) {
    if (entry.isFile) {
      return new Promise((res) =>
        entry.file((f) => {
          state.files.push(new File([f], prefix + f.name, { type: f.type }));
          res();
        })
      );
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      return new Promise((res) => {
        const readAll = () =>
          reader.readEntries(async (batch) => {
            if (!batch.length) return res();
            await Promise.all(batch.map((e) => walkEntry(e, prefix + entry.name + "/")));
            readAll();
          });
        readAll();
      });
    }
    return Promise.resolve();
  }

  function addPicked(fileList) {
    state.files = Array.from(fileList);
    renderFileList();
  }

  function humanSize(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 ** 2) return (n / 1024).toFixed(1) + " KB";
    if (n < 1024 ** 3) return (n / 1024 ** 2).toFixed(1) + " MB";
    return (n / 1024 ** 3).toFixed(2) + " GB";
  }

  function renderFileList() {
    if (!state.files.length) {
      els.uploadList.classList.add("hidden");
      els.btnUpload.disabled = true;
      return;
    }
    els.btnUpload.disabled = false;
    els.uploadList.classList.remove("hidden");
    const total = state.files.reduce((s, f) => s + f.size, 0);
    els.uploadList.innerHTML =
      state.files.slice(0, 50).map((f) =>
        `<div class="row"><span>📄 ${escapeHtml(f.webkitRelativePath || f.name)}</span><span class="size">${humanSize(f.size)}</span></div>`
      ).join("") +
      (state.files.length > 50 ? `<div class="row"><span>… 另有 ${state.files.length - 50} 个文件</span></div>` : "") +
      `<div class="row"><span><b>共 ${state.files.length} 个文件</b></span><span class="size">${humanSize(total)}</span></div>`;
    setStatus(els.uploadStatus, `已选择 ${state.files.length} 个文件`, "");
  }

  // ----------------------------------------------------------------- upload
  els.btnUpload.addEventListener("click", async () => {
    if (!state.files.length) return;
    els.btnUpload.disabled = true;
    setStatus(els.uploadStatus, "上传中…", "");
    const fd = new FormData();
    state.files.forEach((f) => fd.append("files", f, f.webkitRelativePath || f.name));
    try {
      const resp = await fetch("/api/jobs", { method: "POST", body: fd });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "上传失败");
      state.jobId = data.job_id;
      setStatus(els.uploadStatus, `扫描完成：${data.file_count} 个 DICOM 实例`, "ok");
      await enterConfig(data);
    } catch (err) {
      setStatus(els.uploadStatus, err.message, "error");
      els.btnUpload.disabled = false;
    }
  });

  // ------------------------------------------------------- config / preview
  async function enterConfig(jobData) {
    showStep(2);
    await loadProfiles();
    await loadPreview();
  }

  async function loadProfiles() {
    const resp = await fetch("/api/profiles");
    const profiles = await resp.json();
    els.profileCards.innerHTML = profiles.map((p) => `
      <div class="profile-card ${p.key === state.profile ? "selected" : ""}" data-key="${p.key}">
        <h3>${p.label}</h3>
        <p>${p.description}</p>
      </div>`).join("");
    $$(".profile-card").forEach((card) =>
      card.addEventListener("click", () => {
        state.profile = card.dataset.key;
        $$(".profile-card").forEach((c) => c.classList.toggle("selected", c.dataset.key === state.profile));
        loadPreview();
      })
    );
  }

  async function loadPreview() {
    if (!state.jobId) return;
    setStatus(els.previewStatus, "生成预览…", "");
    els.btnAnon.disabled = true;
    try {
      const resp = await fetch(`/api/jobs/${state.jobId}/preview?profile=${encodeURIComponent(state.profile)}`);
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "预览失败");
      state.preview = data;
      renderPreview(data);
      setStatus(els.previewStatus, "预览已生成，可执行匿名化", "ok");
      els.btnAnon.disabled = false;
    } catch (err) {
      setStatus(els.previewStatus, err.message, "error");
    }
  }

  function renderPreview(data) {
    const s = data.summary;
    els.summary.innerHTML = `
      <div class="metric"><b>${s.dicom_files}</b><span>DICOM 文件</span></div>
      <div class="metric"><b>${s.patients}</b><span>患者</span></div>
      <div class="metric"><b>${s.studies}</b><span>检查</span></div>
      <div class="metric"><b>${s.series}</b><span>序列</span></div>
      <div class="metric"><b>${s.changed_fields}</b><span>变更字段类型</span></div>
      <div class="metric"><b>${s.files_with_private_tags}</b><span>含私有标签文件</span></div>`;

    els.hierarchy.innerHTML = data.hierarchy.map((p) => `
      <div class="patient-block">
        <h4>患者：${escapeHtml(p.name || "（无名）")} <span class="pill">${escapeHtml(p.patient_id || "无 ID")}</span></h4>
        <div class="uidline">PatientUID ${uidShort(p.old_uid)} <span class="arrow">→</span> ${uidShort(p.new_uid)}</div>
        ${p.studies.map((st) => `
          <div class="study-block">
            <div class="uidline">Study ${st.date ? escapeHtml(st.date) + " · " : ""}${uidShort(st.old_uid)} <span class="arrow">→</span> ${uidShort(st.new_uid)}</div>
            ${st.series.map((sr) => `
              <div class="series-row">
                <span class="pill">${escapeHtml(sr.modality || "—")}</span>
                <span>${sr.instances} 个实例</span>
                <span class="uidline">${uidShort(sr.old_uid)} <span class="arrow">→</span> ${uidShort(sr.new_uid)}</span>
              </div>`).join("")}
          </div>`).join("")}
      </div>`).join("");

    const actionLabel = {
      blanked: "清空 / 移除", date_cleared: "日期清空", uid_remapped: "UID 重写",
      private_removed: "私有标签移除", added: "新增审计字段", unchanged: "未变更",
    };
    els.tabChanges.innerHTML = data.changes.length ? `
      <table>
        <thead><tr><th>动作</th><th>标签</th><th>变更前（示例）</th><th>变更后</th><th>影响</th></tr></thead>
        <tbody>
        ${data.changes.map((c) => `
          <tr>
            <td><span class="badge ${c.action}">${actionLabel[c.action] || c.action}</span></td>
            <td><div class="tagcode">${c.tag}</div><div>${escapeHtml(c.name)}</div><div class="file-cell">${escapeHtml(c.keyword)} · ${c.vr}</div></td>
            <td class="before mono">${escapeHtml(c.before)}</td>
            <td class="after mono">${escapeHtml(c.after)}</td>
            <td class="file-cell">${c.file_count} 个文件</td>
          </tr>`).join("")}
        </tbody>
      </table>` : `<div class="empty">该配置下未检测到需要变更的字段</div>`;

    els.tabUids.innerHTML = data.uid_remaps.length ? `
      <table>
        <thead><tr><th>层级</th><th>原 UID</th><th></th><th>伪名 UID</th></tr></thead>
        <tbody>
        ${data.uid_remaps.map((u) => `
          <tr><td>${u.domain}</td><td class="mono">${u.old}</td><td class="arrow">→</td><td class="mono">${u.new}</td></tr>`).join("")}
        </tbody>
      </table>` : `<div class="empty">无 UID 映射</div>`;

    els.tabFiles.innerHTML = `
      <table>
        <thead><tr><th>文件</th><th>模态</th><th>原患者</th><th>SOPInstanceUID</th><th>状态</th></tr></thead>
        <tbody>
        ${data.files.map((f) => `
          <tr>
            <td>${escapeHtml(f.name)}</td>
            <td>${escapeHtml(f.modality || "")}</td>
            <td class="mono">${escapeHtml(f.patient_name || "")} ${f.patient_id ? "· " + escapeHtml(f.patient_id) : ""}</td>
            <td class="file-cell">${f.old_sop_uid ? uidShort(f.old_sop_uid) + ` <span class="arrow">→</span> ` + uidShort(f.new_sop_uid) : ""}</td>
            <td>${f.error ? `<span class="badge error">解析失败</span> <span class="file-cell">${escapeHtml(f.error)}</span>` : `<span class="badge added">就绪</span>`}</td>
          </tr>`).join("")}
        </tbody>
      </table>`;
  }

  $$(".tab").forEach((tab) =>
    tab.addEventListener("click", () => {
      $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      ["changes", "uids", "files"].forEach((name) =>
        $(`#tab-${name}`).classList.toggle("hidden", name !== tab.dataset.tab)
      );
    })
  );

  // -------------------------------------------------------------- anonymize
  els.btnAnon.addEventListener("click", async () => {
    els.btnAnon.disabled = true;
    setStatus(els.previewStatus, "正在重写标签与 UID…", "");
    const fd = new FormData();
    fd.append("profile", state.profile);
    try {
      const resp = await fetch(`/api/jobs/${state.jobId}/anonymize`, { method: "POST", body: fd });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "匿名化失败");
      els.linkZip.href = data.download_url;
      els.linkReport.href = data.report_url;
      els.doneSummary.textContent =
        `成功写出 ${data.written} 个匿名化实例，ZIP 大小 ${humanSize(data.zip_size)}。`;
      els.doneFailures.innerHTML = (data.failures || []).length
        ? `<div class="empty" style="color:var(--warn)">以下文件被跳过：</div>` +
          data.failures.map((f) => `<div class="frow">· ${escapeHtml(f.name)}: ${escapeHtml(f.error)}</div>`).join("")
        : "";
      showStep(3);
    } catch (err) {
      setStatus(els.previewStatus, err.message, "error");
      els.btnAnon.disabled = false;
    }
  });

  els.btnBack.addEventListener("click", () => { resetAll(); showStep(1); });
  els.btnAgain.addEventListener("click", () => showStep(2));
  els.btnRestart.addEventListener("click", () => { resetAll(); showStep(1); });

  function resetAll() {
    state.files = []; state.jobId = null; state.preview = null;
    els.fileInput.value = ""; els.fileInputFiles.value = "";
    els.uploadList.classList.add("hidden");
    els.btnUpload.disabled = true;
    setStatus(els.uploadStatus, "", "");
  }

  // ---------------------------------------------------------------- helpers
  function setStatus(el, msg, kind) {
    el.textContent = msg;
    el.className = "status" + (kind ? " " + kind : "");
  }

  function uidShort(uid) {
    if (!uid) return "—";
    return uid.length > 26 ? uid.slice(0, 24) + "…" : uid;
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  showStep(1);
})();
