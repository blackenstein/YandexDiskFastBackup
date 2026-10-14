// --- Форматирование ---

const UNITS = ["Б", "КБ", "МБ", "ГБ", "ТБ"];

export function formatBytes(n) {
  if (n === null || n === undefined || isNaN(n)) return "—";
  if (n === 0) return "0 Б";
  let v = Number(n);
  let i = 0;
  while (v >= 1024 && i < UNITS.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${UNITS[i]}`;
}

export function formatSpeed(bps) {
  if (!bps || bps <= 0) return "";
  return `${formatBytes(bps)}/с`;
}

export function formatDate(s) {
  if (!s) return "—";
  const d = new Date(s);
  if (isNaN(d)) return s;
  return d.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// --- SVG Иконки ---

export function DiskLogo(size = 28) {
  return `
    <svg width="${size}" height="${size}" viewBox="0 0 32 32" aria-hidden="true">
      <circle cx="16" cy="16" r="15" fill="#FFCC00" />
      <circle cx="16" cy="16" r="5.5" fill="#FFFFFF" />
    </svg>
  `;
}

export function FolderIcon(size = 20) {
  return `
    <svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M3 6a2 2 0 0 1 2-2h4.2l2 2H19a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6z"
        fill="#FFCC00"
        stroke="#DFAE00"
        strokeWidth="1"
      />
    </svg>
  `;
}

export function FileIcon(size = 20) {
  return `
    <svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M6 2h8l6 6v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z"
        fill="#F2F2F2"
        stroke="#C9C9C9"
        strokeWidth="1"
      />
      <path d="M14 2v6h6" stroke="#C9C9C9" strokeWidth="1" fill="none" />
    </svg>
  `;
}

// --- Управление темой ---

function initTheme() {
  const saved = localStorage.getItem("ydl_theme");
  if (saved) {
    document.documentElement.setAttribute("data-theme", saved);
  } else if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
    document.documentElement.setAttribute("data-theme", "dark");
  } else {
    document.documentElement.setAttribute("data-theme", "light");
  }
}

function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme") || "light";
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("ydl_theme", next);
  updateThemeButton();
}

function updateThemeButton() {
  const btn = document.getElementById("btn-theme-toggle");
  if (!btn) return;
  const isDark = document.documentElement.getAttribute("data-theme") === "dark";
  btn.innerHTML = isDark ? "☀️ Светлая" : "🌙 Тёмная";
  btn.title = isDark ? "Включить светлую тему" : "Включить тёмную тему";
}

// --- Клиент API ---

async function request(url, options = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.error || `Ошибка ${res.status}`);
  }
  return data;
}

export const api = {
  tokenStatus: () => request("/api/token/"),
  saveToken: (token) => request("/api/token/", { method: "POST", body: JSON.stringify({ token }) }),
  deleteToken: () => request("/api/token/", { method: "DELETE" }),
  oauthUrl: () => request("/api/oauth-url/"),
  browse: (path) => request(`/api/disk/browse/?path=${encodeURIComponent(path)}`),
  tasks: () => request("/api/tasks/"),
  createTask: (dest, items, concurrency, keep_parts = false) =>
    request("/api/tasks/", { method: "POST", body: JSON.stringify({ dest, items, concurrency, keep_parts }) }),
  cancelTask: (id) => request(`/api/tasks/${id}/cancel/`, { method: "POST" }),
  deleteTask: (id) => request(`/api/tasks/${id}/`, { method: "DELETE" }),
  browseFolder: () => request("/api/browse-folder/", { method: "POST" }),
};

// --- Состояние приложения ---

const state = {
  token: undefined, // undefined = загрузка, null = не подключён
  tasks: [],
  selected: {},
  browser: {
    path: "/",
    items: [],
    loading: true,
    error: null,
  },
  taskPanel: {
    dest: "./downloads",
    concurrency: 8,
    keepParts: false,
    busy: false,
    msg: null,
    msgError: null,
    showErrorsMap: {}, // task_id -> boolean
  },
  tokenScreen: {
    tokenInput: "",
    oauthUrl: "",
    busy: false,
    error: null,
  },
};

const STATUS_CONFIG = {
  pending: { label: "В очереди", cls: "st-pending" },
  running: { label: "Выгрузка…", cls: "st-running" },
  done: { label: "Завершено", cls: "st-done" },
  error: { label: "Ошибка", cls: "st-error" },
  cancelled: { label: "Отменено", cls: "st-cancelled" },
};

// --- Рендеринг ---

const root = document.getElementById("root");

function render() {
  if (state.token === undefined) {
    root.innerHTML = `<div class="loading">Загрузка…</div>`;
    return;
  }

  if (!state.token || !state.token.connected) {
    renderTokenScreen();
    return;
  }

  renderMainApp();
}

function renderTokenScreen() {
  const ts = state.tokenScreen;
  root.innerHTML = `
    <div class="token-screen">
      <div class="token-card">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
          <div class="token-logo" style="margin:0;">
            ${DiskLogo(42)}
            <h1 style="font-size:18px;">Выгрузка из Яндекс Диска</h1>
          </div>
          <button id="btn-theme-toggle" class="theme-toggle-btn"></button>
        </div>
        <p class="muted">
          Массовое асинхронное скачивание файлов и папок на локальный диск с сохранением
          структуры вложенных каталогов.
        </p>
        ${
          ts.oauthUrl
            ? `
          <p class="hint">
            1. Откройте ссылку авторизации:
            <a href="${ts.oauthUrl}" target="_blank" rel="noreferrer">получить токен</a><br />
            2. Скопируйте токен из адресной строки и вставьте его ниже.
          </p>
        `
            : ""
        }
        <form id="token-form">
          <textarea
            id="token-textarea"
            class="token-input"
            placeholder="Вставьте токен (y0_Ag…)"
            rows="4"
          >${ts.tokenInput}</textarea>
          ${ts.error ? `<div class="alert error">${ts.error}</div>` : ""}
          <button
            type="submit"
            class="btn btn-primary btn-block"
            ${ts.busy || !ts.tokenInput.trim() ? "disabled" : ""}
          >
            ${ts.busy ? "Подключаем…" : "Подключить диск"}
          </button>
        </form>
      </div>
    </div>
  `;

  updateThemeButton();
  document.getElementById("btn-theme-toggle")?.addEventListener("click", toggleTheme);

  const textarea = document.getElementById("token-textarea");
  textarea.addEventListener("input", (e) => {
    ts.tokenInput = e.target.value;
    const submitBtn = root.querySelector("button[type=submit]");
    if (submitBtn) {
      submitBtn.disabled = ts.busy || !ts.tokenInput.trim();
    }
  });

  const form = document.getElementById("token-form");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    ts.busy = true;
    ts.error = null;
    render();
    try {
      await api.saveToken(ts.tokenInput);
      await loadToken();
    } catch (err) {
      ts.error = err.message;
      ts.busy = false;
      render();
    }
  });
}

function renderMainApp() {
  const token = state.token;
  root.innerHTML = `
    <div class="app">
      <header class="topbar">
        <span class="logo">
          ${DiskLogo(26)}
          <span>Яндекс Диск</span>
        </span>
        <span class="sep"></span>
        <span class="subtitle">Выгрузка на диск</span>
        <span class="spacer"></span>
        <span class="account">
          ${token.login || "Диск"} · занято ${formatBytes(token.used_bytes)} из ${formatBytes(token.total_bytes)} · ${token.token_masked}
        </span>
        <button id="btn-theme-toggle" class="theme-toggle-btn"></button>
        <button id="btn-disconnect" class="btn btn-ghost btn-small">
          Отключить
        </button>
      </header>

      <div class="main">
        <div id="browser-container" class="browser"></div>
        <div id="task-panel-container" class="task-panel"></div>
      </div>
    </div>
  `;

  updateThemeButton();
  document.getElementById("btn-theme-toggle")?.addEventListener("click", toggleTheme);

  document.getElementById("btn-disconnect").addEventListener("click", async () => {
    await api.deleteToken();
    await loadToken();
  });

  renderBrowser();
  initTaskPanelLayout();
  updateTasksListOnly();
}

function renderBrowser() {
  const container = document.getElementById("browser-container");
  if (!container) return;

  const b = state.browser;
  const crumbs = b.path.split("/").filter(Boolean);
  const selectedCount = Object.keys(state.selected).length;
  const allSelected = b.items.length > 0 && b.items.every((i) => Boolean(state.selected[i.path]));

  let crumbsHtml = `
    <button class="crumb" data-path="/">Диск</button>
  `;
  crumbs.forEach((c, i) => {
    const p = "/" + crumbs.slice(0, i + 1).join("/");
    crumbsHtml += `
      <span class="crumb-sep">
        <span class="crumb-sep-mark">/</span>
        <button class="crumb" data-path="${p}">${c}</button>
      </span>
    `;
  });

  container.innerHTML = `
    <div class="breadcrumbs">
      ${crumbsHtml}
      <span class="spacer"></span>
      <button id="btn-refresh-browse" class="btn btn-ghost btn-small" title="Обновить">
        ⟳ Обновить
      </button>
    </div>

    ${b.error ? `<div class="alert error browse-alert">${b.error}</div>` : ""}

    ${
      b.loading
        ? `<div class="loading">Загрузка…</div>`
        : `
      <div class="table-wrap">
        <table class="file-table">
          <thead>
            <tr>
              <th class="col-check">
                <input type="checkbox" id="check-all" ${allSelected ? "checked" : ""} />
              </th>
              <th>Имя</th>
              <th class="col-size">Размер</th>
              <th class="col-date">Изменён</th>
            </tr>
          </thead>
          <tbody>
            ${
              b.items.length === 0
                ? `<tr><td colspan="4" class="empty">Папка пуста</td></tr>`
                : b.items
                    .map((item) => {
                      const isSel = Boolean(state.selected[item.path]);
                      return `
                  <tr class="${isSel ? "selected" : ""}" data-path="${item.path}" data-type="${item.type}">
                    <td class="col-check">
                      <input type="checkbox" class="item-check" data-path="${item.path}" data-type="${item.type}" ${isSel ? "checked" : ""} />
                    </td>
                    <td>
                      <span class="file-name ${item.type === "folder" ? "clickable" : ""}" data-path="${item.path}" data-type="${item.type}">
                        ${item.type === "folder" ? FolderIcon() : FileIcon()}
                        ${item.name}
                      </span>
                    </td>
                    <td class="col-size">${item.type === "file" ? formatBytes(item.size) : "—"}</td>
                    <td class="col-date">${formatDate(item.modified)}</td>
                  </tr>
                `;
                    })
                    .join("")
            }
          </tbody>
        </table>
      </div>
    `
    }

    ${
      selectedCount > 0
        ? `
      <div class="selection-bar">
        <span>Выбрано: <b>${selectedCount}</b></span>
        <button id="btn-clear-sel" class="btn btn-ghost btn-small">Сбросить</button>
      </div>
    `
        : ""
    }
  `;

  // Слушатели крошек
  container.querySelectorAll(".crumb").forEach((btn) => {
    btn.addEventListener("click", () => loadBrowse(btn.dataset.path));
  });

  const refreshBtn = container.querySelector("#btn-refresh-browse");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => loadBrowse(b.path));
  }

  // Чекбокс «Выбрать все»
  const checkAll = container.querySelector("#check-all");
  if (checkAll) {
    checkAll.addEventListener("change", () => {
      if (allSelected) {
        state.selected = {};
      } else {
        b.items.forEach((item) => {
          state.selected[item.path] = { path: item.path, type: item.type };
        });
      }
      renderBrowser();
      updateTasksListOnly();
    });
  }

  // Чекбоксы отдельных файлов
  container.querySelectorAll(".item-check").forEach((cb) => {
    cb.addEventListener("change", () => {
      const p = cb.dataset.path;
      const t = cb.dataset.type;
      if (state.selected[p]) {
        delete state.selected[p];
      } else {
        state.selected[p] = { path: p, type: t };
      }
      renderBrowser();
      updateTasksListOnly();
    });
  });

  // Клик по имени папки
  container.querySelectorAll(".file-name.clickable").forEach((span) => {
    span.addEventListener("click", () => {
      loadBrowse(span.dataset.path);
    });
  });

  // Двойной клик по строке папки
  container.querySelectorAll("tr").forEach((tr) => {
    if (tr.dataset.type === "folder") {
      tr.addEventListener("dblclick", () => {
        loadBrowse(tr.dataset.path);
      });
    }
  });

  // Сброс выбора
  const clearSelBtn = container.querySelector("#btn-clear-sel");
  if (clearSelBtn) {
    clearSelBtn.addEventListener("click", () => {
      state.selected = {};
      renderBrowser();
      updateTasksListOnly();
    });
  }
}

// Инициализирует статическую разметку панели задач один раз (без лагов при наборе текста)
function initTaskPanelLayout() {
  const panel = document.getElementById("task-panel-container");
  if (!panel) return;

  const tp = state.taskPanel;

  panel.innerHTML = `
    <h2>Выгрузка</h2>

    <label class="field-label" for="dest-input">
      Папка на локальном диске
    </label>
    <div class="input-group">
      <input
        id="dest-input"
        class="text-input"
        value="${tp.dest}"
        placeholder="./downloads или C:\\Downloads"
        spellCheck="false"
      />
      <button id="btn-browse-folder" class="btn btn-ghost" title="Выбрать папку через диалог проводника">
        📁 Обзор…
      </button>
    </div>
    <div style="display:flex; justify-content:space-between; align-items:center; margin-top:10px;">
      <label class="field-label" for="concurrency-input" style="margin:0;">
        Параллельных воркеров (потоков):
      </label>
      <input
        id="concurrency-input"
        type="number"
        min="1"
        max="32"
        class="text-input"
        style="width: 76px; padding: 5px 8px; text-align: center;"
        value="${tp.concurrency}"
      />
    </div>
    <div style="margin-top:12px; padding:10px 12px; background:var(--bg-subtle, rgba(0,0,0,0.02)); border:1px solid var(--border, #e5e7eb); border-radius:8px;">
      <label style="display:flex; align-items:flex-start; gap:8px; cursor:pointer; font-size:13px; color:inherit;">
        <input
          id="keep-parts-checkbox"
          type="checkbox"
          style="margin-top:2px; cursor:pointer;"
          ${tp.keepParts ? "checked" : ""}
        />
        <span>
          <strong>Сохранять недокачанные файлы (.part)</strong>
          <span class="hint small" style="display:block; margin-top:3px; font-size:11.5px; line-height:1.35; opacity:0.85;">
            Сохранение недокачанных файлов в надежде на то, что они докачаются после восстановления инфраструктуры Яндекса
          </span>
        </span>
      </label>
    </div>
    <p class="hint small" style="margin-top:8px;">
      Можно указать любой путь на любом диске. Уже скачанные файлы пропускаются автоматически.
    </p>

    <button id="btn-start-download" class="btn btn-primary btn-block" disabled>
      Скачать выбранное (0)
    </button>

    <div id="panel-alerts-container"></div>
    <div id="tasks-list-container" class="tasks-list"></div>
  `;

  const destInput = panel.querySelector("#dest-input");
  destInput.addEventListener("input", (e) => {
    tp.dest = e.target.value;
  });

  const concInput = panel.querySelector("#concurrency-input");
  concInput.addEventListener("input", (e) => {
    tp.concurrency = parseInt(e.target.value) || 8;
  });

  const keepPartsCb = panel.querySelector("#keep-parts-checkbox");
  if (keepPartsCb) {
    keepPartsCb.addEventListener("change", (e) => {
      tp.keepParts = e.target.checked;
    });
  }

  const browseBtn = panel.querySelector("#btn-browse-folder");
  browseBtn.addEventListener("click", async () => {
    browseBtn.disabled = true;
    browseBtn.innerText = "⏳ Выбор…";
    try {
      const res = await api.browseFolder();
      if (res && res.path) {
        tp.dest = res.path;
        destInput.value = res.path;
      }
    } catch (e) {
      console.warn("Folder dialog error:", e);
    } finally {
      browseBtn.disabled = false;
      browseBtn.innerText = "📁 Обзор…";
    }
  });

  const startBtn = panel.querySelector("#btn-start-download");
  startBtn.addEventListener("click", async () => {
    tp.busy = true;
    tp.msg = null;
    tp.msgError = null;
    updateTasksListOnly();
    try {
      const items = Object.values(state.selected);
      await api.createTask(tp.dest, items, tp.concurrency, tp.keepParts);
      state.selected = {};
      tp.msg = "Выгрузка запущена";
      await refreshTasks();
      renderBrowser();
    } catch (err) {
      tp.msgError = err.message;
    } finally {
      tp.busy = false;
      updateTasksListOnly();
    }
  });
}

// Обновляет ТОЛЬКО список задач и кнопку запуска без перерисовки поля ввода
function updateTasksListOnly() {
  const panel = document.getElementById("task-panel-container");
  if (!panel) return;

  const tp = state.taskPanel;
  const count = Object.keys(state.selected).length;

  // Обновляем кнопку старта
  const startBtn = panel.querySelector("#btn-start-download");
  if (startBtn) {
    startBtn.disabled = tp.busy || count === 0;
    startBtn.innerText = tp.busy ? "Запускаем…" : `Скачать выбранное (${count})`;
  }

  // Обновляем сообщения
  const alertsContainer = panel.querySelector("#panel-alerts-container");
  if (alertsContainer) {
    alertsContainer.innerHTML = `
      ${tp.msg ? `<div class="alert ok">${tp.msg}</div>` : ""}
      ${tp.msgError ? `<div class="alert error">${tp.msgError}</div>` : ""}
    `;
  }

  // Обновляем карточки задач
  const listContainer = panel.querySelector("#tasks-list-container");
  if (!listContainer) return;

  if (state.tasks.length === 0) {
    listContainer.innerHTML = `<p class="muted">Задач пока нет</p>`;
  } else {
    listContainer.innerHTML = state.tasks.map((task) => renderTaskCard(task)).join("");
    attachTaskCardEvents(listContainer);
  }
}

function renderTaskCard(task) {
  const st = STATUS_CONFIG[task.status] || STATUS_CONFIG.pending;
  // Прогресс строго по количеству обработанных файлов для стабильности и предсказуемости
  const pct =
    task.total_files > 0
      ? Math.min(100, (task.done_files / task.total_files) * 100)
      : 0;
  const active = task.status === "running" || task.status === "pending";

  const label =
    task.failed_files > 0 && (task.status === "done" || task.status === "error")
      ? `С ошибками: ${task.failed_files}`
      : st.label;

  const showErrors = Boolean(state.taskPanel.showErrorsMap[task.id]);
  const speed = task.speed || 0;

  return `
    <div class="task-card task-${task.status}">
      <div class="task-head">
        <span class="task-title" title="${task.dest}">${task.dest}</span>
        <div style="display:flex; align-items:center; gap:6px;">
          ${task.keep_parts ? `<span title="Сохранение недокачанных .part включено" style="font-size:11px; font-weight:600; padding:2px 6px; border-radius:4px; background:#e0f2fe; color:#0369a1;">.part</span>` : ""}
          <span class="chip ${st.cls}">${label}</span>
        </div>
      </div>

      ${
        task.total_files > 0 || active
          ? `
        <div class="progress">
          <div class="progress-fill" style="width: ${pct}%"></div>
        </div>
        <div class="task-meta">
          <span>${task.done_files} / ${task.total_files || "…"} файлов (${Math.round(pct)}%)</span>
          <span>${formatBytes(task.done_bytes)} из ${formatBytes(task.total_bytes)}</span>
          ${task.status === "running" && speed > 0 ? `<span>⚡ ${formatSpeed(speed)}</span>` : ""}
        </div>
      `
          : ""
      }

      ${
        (task.error && task.error.includes("429")) || (task.errors && task.errors.some((e) => e.error && e.error.includes("429")))
          ? `
        <div style="background: #fff3cd; color: #856404; border: 1px solid #ffeeba; border-radius: 6px; padding: 8px 12px; margin: 8px 0; font-size: 13px; line-height: 1.4;">
          ⚠️ <strong>Превышен лимит запросов Яндекса (HTTP 429).</strong><br>
          Рекомендуется снизить количество воркеров (потоков) в настройках выгрузки до <strong>4–6</strong>.
        </div>
      `
          : ""
      }

      ${task.error ? `<div class="task-error">${task.error}</div>` : ""}

      ${
        task.errors && task.errors.length > 0
          ? `
        <button class="link-btn btn-toggle-errors" data-id="${task.id}">
          ${showErrors ? "Скрыть ошибки" : `Ошибки (${task.errors.length})`}
        </button>
        ${
          showErrors
            ? `
          <ul class="task-errors">
            ${task.errors.map((e) => `<li><code>${e.path}</code> — ${e.error}</li>`).join("")}
          </ul>
        `
            : ""
        }
      `
          : ""
      }

      <div class="task-actions">
        ${
          active
            ? `
          <button class="btn btn-small btn-ghost btn-cancel-task" data-id="${task.id}">
            Отменить
          </button>
        `
            : `
          ${
            task.failed_files > 0 && task.errors && task.errors.length > 0
              ? `
            <button
              class="btn btn-small btn-primary btn-retry-task"
              data-id="${task.id}"
              title="Создаст новую задачу с файлами, которые не скачались"
            >
              Повторить неудавшиеся (${task.errors.length})
            </button>
          `
              : ""
          }
          <button class="btn btn-small btn-ghost btn-del-task" data-id="${task.id}">
            Убрать
          </button>
        `
        }
      </div>
    </div>
  `;
}

function attachTaskCardEvents(container) {
  container.querySelectorAll(".btn-toggle-errors").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.id;
      state.taskPanel.showErrorsMap[id] = !state.taskPanel.showErrorsMap[id];
      updateTasksListOnly();
    });
  });

  container.querySelectorAll(".btn-cancel-task").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api.cancelTask(btn.dataset.id).catch(() => {});
      await refreshTasks();
    });
  });

  container.querySelectorAll(".btn-del-task").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api.deleteTask(btn.dataset.id).catch(() => {});
      await refreshTasks();
    });
  });

  container.querySelectorAll(".btn-retry-task").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const task = state.tasks.find((t) => t.id === btn.dataset.id);
      if (task && task.errors) {
        const items = task.errors.map((e) => ({ path: e.path, type: "file" }));
        const keepParts = Boolean(task.keep_parts);
        await api.createTask(task.dest, items, state.taskPanel.concurrency, keepParts).catch(() => {});
        await refreshTasks();
      }
    });
  });
}

// --- Загрузка данных ---

async function loadToken() {
  try {
    state.token = await api.tokenStatus();
    if (!state.token.connected) {
      api.oauthUrl().then((d) => {
        state.tokenScreen.oauthUrl = d.url;
        render();
      }).catch(() => {});
    }
  } catch {
    state.token = null;
  }
  render();
  if (state.token && state.token.connected) {
    loadBrowse("/");
    refreshTasks();
  }
}

async function loadBrowse(path = "/") {
  const b = state.browser;
  b.loading = true;
  b.error = null;
  renderBrowser();
  try {
    const data = await api.browse(path);
    b.path = data.path || path;
    b.items = data.items || [];
  } catch (err) {
    b.error = err.message;
    b.items = [];
  } finally {
    b.loading = false;
    renderBrowser();
  }
}

async function refreshTasks() {
  try {
    const d = await api.tasks();
    state.tasks = d.tasks || [];
    updateTasksListOnly();
  } catch {
    // игнорируем ошибку опроса
  }
}

// --- Инициализация ---

initTheme();
loadToken();
setInterval(refreshTasks, 2000);
