(function () {
  "use strict";

  var STORAGE_KEY = "cookie_consent";
  var MAX_AGE = 60 * 60 * 24 * 365;
  var banner = document.getElementById("cookie-banner");
  var settings = document.getElementById("cookie-settings");
  if (!banner || !settings) return;

  var version = banner.dataset.consentVersion || "1";
  var analyticsEnabled = banner.dataset.analyticsEnabled === "true";
  var analytics = settings.querySelector("#cookie-analytics");
  var lastFocus = null;

  function parse(value) {
    if (!value) return null;
    try {
      var result = JSON.parse(value);
      return result && typeof result === "object" ? result : null;
    } catch (_) {
      return null;
    }
  }

  function readCookie() {
    var match = document.cookie.match(/(?:^|;\s*)cookie_consent=([^;]+)/);
    if (!match) return null;
    try {
      return parse(decodeURIComponent(match[1]));
    } catch (_) {
      return null;
    }
  }

  function readConsent() {
    var local = null;
    try {
      local = parse(window.localStorage.getItem(STORAGE_KEY));
    } catch (_) {}
    var value = local || readCookie();
    return value && value.version === version ? value : null;
  }

  function persist(allowAnalytics) {
    var analyticsAllowed = analyticsEnabled && Boolean(allowAnalytics);
    var value = {
      version: version,
      necessary: true,
      analytics: analyticsAllowed,
      decided_at: new Date().toISOString()
    };
    var serialized = JSON.stringify(value);
    try {
      window.localStorage.setItem(STORAGE_KEY, serialized);
    } catch (_) {}
    document.cookie = STORAGE_KEY + "=" + encodeURIComponent(serialized)
      + "; path=/; max-age=" + MAX_AGE + "; SameSite=Lax"
      + (window.location.protocol === "https:" ? "; Secure" : "");
    banner.hidden = true;
    closeSettings();
    window.dispatchEvent(new CustomEvent("cookieconsentchange", {detail: value}));
    if (analyticsEnabled && value.analytics && typeof window.__loadAnalytics === "function") {
      window.__loadAnalytics();
    }
  }

  function openSettings() {
    var current = readConsent();
    if (analytics) analytics.checked = Boolean(current && current.analytics);
    lastFocus = document.activeElement;
    settings.hidden = false;
    document.body.classList.add("cookie-settings-open");
    settings.querySelector(".cookie-settings__close").focus();
  }

  function closeSettings() {
    if (settings.hidden) return;
    settings.hidden = true;
    document.body.classList.remove("cookie-settings-open");
    if (lastFocus && typeof lastFocus.focus === "function") lastFocus.focus();
  }

  document.addEventListener("click", function (event) {
    var manage = event.target.closest("[data-cookie-manage]");
    if (manage) {
      event.preventDefault();
      openSettings();
      return;
    }
    var button = event.target.closest("[data-cookie-action]");
    if (!button) return;
    var action = button.dataset.cookieAction;
    if (action === "accept") persist(true);
    if (action === "necessary") persist(false);
    if (action === "settings") openSettings();
    if (action === "save") persist(analytics && analytics.checked);
    if (action === "close") closeSettings();
  });

  settings.addEventListener("click", function (event) {
    if (event.target === settings) closeSettings();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !settings.hidden) closeSettings();
  });

  var currentConsent = readConsent();
  banner.hidden = Boolean(currentConsent);
  if (
    analyticsEnabled
    && currentConsent
    && currentConsent.analytics
    && typeof window.__loadAnalytics === "function"
  ) {
    window.__loadAnalytics();
  }
  window.cookieConsent = {open: openSettings, read: readConsent};
})();
