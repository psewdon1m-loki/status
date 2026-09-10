const LANGUAGE_STORAGE_KEY = "cake-project.language";
const REFRESH_INTERVAL_SECONDS = 30;

const componentList = document.querySelector("#componentList");
const overallCard = document.querySelector("#overallCard");
const overallStatus = document.querySelector("#overallStatus");
const statusHeadline = document.querySelector("#statusHeadline");
const summaryText = document.querySelector("#summaryText");
const checkedAt = document.querySelector("#checkedAt");
const stateMarker = document.querySelector("#stateMarker");
const channelText = document.querySelector("#channelText");
const designTime = document.querySelector("#designTime");
const componentsMeta = document.querySelector("#componentsMeta");
const refreshCountdown = document.querySelector("#refreshCountdown");
const descriptionMeta = document.querySelector('meta[name="description"]');
const languageButtons = [...document.querySelectorAll("[data-language]")];
const homeLinks = [...document.querySelectorAll("[data-home-link]")];

let currentLanguage = "ru";
let currentData = null;
let secondsUntilRefresh = REFRESH_INTERVAL_SECONDS;
let designTimeFormatter;
let accessibleTimeFormatter;

const translations = {
  ru: {
    title: "Cake Project — статус сервисов",
    description: "Текущее состояние сервисов Cake Project.",
    backAria: "Вернуться на главную страницу Cake Project",
    eyebrow: "СТАТУС ИНФРАСТРУКТУРЫ / LIVE",
    overallCaption: "ОБЩЕЕ СОСТОЯНИЕ",
    autoRefresh: "АВТООБНОВЛЕНИЕ",
    secondsShort: "СЕК",
    componentsTitle: "КОНТРОЛИРУЕМЫЕ КОМПОНЕНТЫ / 4",
    loading: "Загружаем состояние сервисов…",
    back: "ПЕРСОНАЛЬНОЕ ПОДКЛЮЧЕНИЕ",
    languageAria: "Выбор языка",
    noComponents: "Компоненты пока не настроены.",
    lastCheck: "ПОСЛЕДНЯЯ ПРОВЕРКА",
    neverChecked: "ПРОВЕРКА ЕЩЁ НЕ ВЫПОЛНЯЛАСЬ",
    componentsAvailable: ({ available, total }) => `${available} ИЗ ${total} БЕЗ ОШИБОК`,
    statuses: {
      operational: "Работает",
      degraded: "С ограничениями",
      partial_outage: "Частично недоступно",
      major_outage: "Недоступно",
      unknown: "Нет данных",
    },
    headlines: {
      operational: "Все сервисы<br>работают",
      degraded: "Сервисы работают<br>с ограничениями",
      partial_outage: "Часть сервисов<br>недоступна",
      major_outage: "Сервисы<br>недоступны",
      unknown: "Статус пока<br>неизвестен",
    },
    summaries: {
      operational: "Все контролируемые компоненты работают штатно.",
      degraded: "Некоторые функции могут отвечать медленнее или работать с ограничениями.",
      partial_outage: "Часть функций временно недоступна. Детали по компонентам указаны ниже.",
      major_outage: "Основные функции сервиса временно недоступны.",
      unknown: "Не удалось получить актуальное состояние сервисов. Повторим попытку автоматически.",
    },
    live: {
      operational: "ЗАЩИЩЁННЫЙ КАНАЛ",
      degraded: "ЕСТЬ ОГРАНИЧЕНИЯ",
      partial_outage: "ЧАСТИЧНЫЙ СБОЙ",
      major_outage: "СЕРВИС НЕДОСТУПЕН",
      unknown: "ПОЛУЧАЕМ ДАННЫЕ",
    },
    componentMessages: {
      operational: "Работает штатно",
      degraded: "Работает с ограничениями",
      partial_outage: "Частично недоступно",
      major_outage: "Временно недоступно",
      unknown: "Ожидается проверка",
    },
    componentTitles: {
      connection: "Подключение к Loki",
      specific_connections: "Работоспособность конкретных подключений",
      new_connections: "Создание новых подключений",
      subscriptions: "Получение и обновление подписок",
    },
  },
  en: {
    title: "Cake Project — service status",
    description: "Current status of Cake Project services.",
    backAria: "Return to the Cake Project home page",
    eyebrow: "INFRASTRUCTURE STATUS / LIVE",
    overallCaption: "OVERALL STATUS",
    autoRefresh: "AUTO REFRESH",
    secondsShort: "SEC",
    componentsTitle: "MONITORED COMPONENTS / 4",
    loading: "Loading service status…",
    back: "PERSONAL CONNECTION",
    languageAria: "Choose language",
    noComponents: "No components have been configured yet.",
    lastCheck: "LAST CHECK",
    neverChecked: "NO CHECK HAS RUN YET",
    componentsAvailable: ({ available, total }) => `${available} OF ${total} WITHOUT ERRORS`,
    statuses: {
      operational: "Operational",
      degraded: "Limited",
      partial_outage: "Partial outage",
      major_outage: "Unavailable",
      unknown: "No data",
    },
    headlines: {
      operational: "All services<br>operational",
      degraded: "Services are<br>limited",
      partial_outage: "Some services<br>are unavailable",
      major_outage: "Services are<br>unavailable",
      unknown: "Status is currently<br>unknown",
    },
    summaries: {
      operational: "All monitored components are operating normally.",
      degraded: "Some functions may respond slowly or work with limitations.",
      partial_outage: "Some functions are temporarily unavailable. Component details are shown below.",
      major_outage: "Core service functions are temporarily unavailable.",
      unknown: "Current service status could not be retrieved. We will retry automatically.",
    },
    live: {
      operational: "SECURE CHANNEL",
      degraded: "LIMITED SERVICE",
      partial_outage: "PARTIAL OUTAGE",
      major_outage: "SERVICE UNAVAILABLE",
      unknown: "LOADING STATUS",
    },
    componentMessages: {
      operational: "Operating normally",
      degraded: "Operating with limitations",
      partial_outage: "Partially unavailable",
      major_outage: "Temporarily unavailable",
      unknown: "Awaiting check",
    },
    componentTitles: {
      connection: "Connection to Loki",
      specific_connections: "Selected connection availability",
      new_connections: "New connection creation",
      subscriptions: "Subscription retrieval and updates",
    },
  },
};

function translate(key, variables = {}) {
  const value = key.split(".").reduce((result, part) => result?.[part], translations[currentLanguage]);
  return typeof value === "function" ? value(variables) : value;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function normalizedStatus(status) {
  return translations.ru.statuses[status] ? status : "unknown";
}

function detectedLanguage() {
  try {
    const saved = localStorage.getItem(LANGUAGE_STORAGE_KEY);
    if (saved === "ru" || saved === "en") return saved;
  } catch {
    // Locale detection remains available when storage is blocked.
  }
  const preferred = navigator.languages?.[0] || navigator.language || "en";
  return preferred.toLowerCase().startsWith("ru") ? "ru" : "en";
}

function rebuildTimeFormatters() {
  const locale = currentLanguage === "ru" ? "ru-RU" : "en-GB";
  designTimeFormatter = new Intl.DateTimeFormat(locale, {
    day: "2-digit",
    month: "long",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  });
  accessibleTimeFormatter = new Intl.DateTimeFormat(locale, {
    dateStyle: "long",
    timeStyle: "medium",
  });
}

function updateDesignTime() {
  const now = new Date();
  const parts = Object.fromEntries(designTimeFormatter.formatToParts(now).map(({ type, value }) => [type, value]));
  designTime.textContent = `${parts.day} ${parts.month.toUpperCase()} ${parts.year} ${parts.hour}-${parts.minute}-${parts.second}`;
  designTime.dateTime = now.toISOString();
  designTime.setAttribute("aria-label", accessibleTimeFormatter.format(now));
}

function formatCheckedAt(value) {
  if (!value) return translate("neverChecked");
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return translate("neverChecked");
  return `${translate("lastCheck")} · ${accessibleTimeFormatter.format(date)}`;
}

function statusBadge(status) {
  const normalized = normalizedStatus(status);
  return `<span class="status-badge ${normalized}">${escapeHtml(translate(`statuses.${normalized}`))}</span>`;
}

function componentTitle(component) {
  return translate(`componentTitles.${component.key}`) || component.title || component.key;
}

function componentRow(component, index) {
  const status = normalizedStatus(component.status);
  const details = Array.isArray(component.details) ? component.details : [];
  return `<article class="component-row">
    <span class="component-index" aria-hidden="true">${String(index + 2).padStart(2, "0")}</span>
    <div class="component-main">
      <h3>${escapeHtml(componentTitle(component))}</h3>
      <p>${escapeHtml(translate(`componentMessages.${status}`))}</p>
    </div>
    ${statusBadge(status)}
    ${details.length ? `<div class="component-details">${details.map((item) => `
      <div class="component-detail"><span>${escapeHtml(item.label)}</span>${statusBadge(item.status)}</div>`).join("")}</div>` : ""}
  </article>`;
}

function render(data) {
  const status = normalizedStatus(data?.overallStatus);
  const components = Array.isArray(data?.components) ? data.components : [];
  const available = components.filter((component) => normalizedStatus(component.status) === "operational").length;

  overallCard.className = `overall-card ${status}`;
  overallStatus.textContent = translate(`statuses.${status}`);
  statusHeadline.innerHTML = translate(`headlines.${status}`);
  summaryText.textContent = translate(`summaries.${status}`);
  checkedAt.textContent = formatCheckedAt(data?.checkedAt);
  checkedAt.dateTime = data?.checkedAt || "";
  stateMarker.className = `state-marker ${status}`;
  channelText.textContent = translate(`live.${status}`);
  componentsMeta.textContent = translate("componentsAvailable", { available, total: components.length });
  componentList.innerHTML = components.map(componentRow).join("") || `<div class="loading-row">${escapeHtml(translate("noComponents"))}</div>`;
  try {
    const homeUrl = new URL(data?.homeUrl || "");
    if (["http:", "https:"].includes(homeUrl.protocol)) {
      homeLinks.forEach((link) => { link.href = homeUrl.href; });
    }
  } catch {
    // Keep the safe static fallback when the service omits an absolute home URL.
  }
}

function applyLanguage(language, persist = false) {
  currentLanguage = language === "ru" ? "ru" : "en";
  document.documentElement.lang = currentLanguage;
  document.title = translate("title");
  descriptionMeta.setAttribute("content", translate("description"));

  document.querySelectorAll("[data-i18n]").forEach((element) => {
    element.textContent = translate(element.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-aria-label]").forEach((element) => {
    element.setAttribute("aria-label", translate(element.dataset.i18nAriaLabel));
  });
  languageButtons.forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.language === currentLanguage));
  });

  rebuildTimeFormatters();
  updateDesignTime();
  render(currentData || { overallStatus: "unknown", components: [] });

  if (persist) {
    try {
      localStorage.setItem(LANGUAGE_STORAGE_KEY, currentLanguage);
    } catch {
      // The switch still works for this page view without storage.
    }
  }
}

async function refreshStatus() {
  secondsUntilRefresh = REFRESH_INTERVAL_SECONDS;
  refreshCountdown.textContent = String(secondsUntilRefresh);
  try {
    const response = await fetch("/api/v1/public/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    currentData = await response.json();
  } catch {
    currentData = { overallStatus: "unknown", checkedAt: null, components: [] };
  }
  render(currentData);
}

languageButtons.forEach((button) => {
  button.addEventListener("click", () => applyLanguage(button.dataset.language, true));
});

applyLanguage(detectedLanguage());
refreshStatus();
window.setInterval(updateDesignTime, 1000);
window.setInterval(() => {
  secondsUntilRefresh -= 1;
  if (secondsUntilRefresh <= 0) {
    refreshStatus();
    return;
  }
  refreshCountdown.textContent = String(secondsUntilRefresh);
}, 1000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    updateDesignTime();
    refreshStatus();
  }
});
