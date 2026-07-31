(function () {
  'use strict';

  if (window.__uiEventsBound) return;
  window.__uiEventsBound = true;

  const directCalls = new Set([
    'aiKey', 'aiSend', 'aiToggle', 'clearAllHistory', 'closeCommandPalette',
    'cookieAccept', 'cookieDecline', 'filterConvs', 'goHome', 'heroAction',
    'landingChat', 'landingGoSearch', 'landingKey', 'markAllNotifsRead',
    'markAllRead', 'newChat', 'obNext', 'obPrev', 'obSkip', 'onKey',
    'onSettingChange', 'openCommandPalette', 'pickLang',
    'resumeActiveConversation', 'send', 'toggleLandingTheme',
    'toggleLangMenu', 'toggleMobileNav', 'toggleNotifPanel', 'toggleNotifs',
    'toggleSettingsPanel', 'toggleSidebar', 'toggleTheme', 'toggleVoice',
    'viewAsHelp', 'exitViewAs'
  ]);

  function parseArgs(element, eventName) {
    const raw = element.getAttribute(`data-ui-${eventName}-args`);
    if (!raw) return [];
    try {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (_) {
      return [];
    }
  }

  function callDeclared(element, eventName, event) {
    const name = element.getAttribute(`data-ui-${eventName}`);
    if (!name || !directCalls.has(name) || typeof window[name] !== 'function') return;
    const args = parseArgs(element, eventName);
    if (element.hasAttribute(`data-ui-${eventName}-event`)) args.unshift(event);
    if (element.hasAttribute(`data-ui-${eventName}-value`)) args.push(element.value);
    if (element.hasAttribute(`data-ui-${eventName}-checked`)) args.push(Boolean(element.checked));
    if (element.hasAttribute(`data-ui-${eventName}-self`)) args.push(element);
    window[name](...args);
  }

  function handleSpecialClick(element, event) {
    const action = element.dataset.uiAction;
    if (!action) return false;
    if (action === 'reload') {
      window.location.reload();
    } else if (action === 'click-target') {
      document.getElementById(element.dataset.uiTarget || '')?.click();
    } else if (action === 'navigate') {
      window.location.assign(element.dataset.uiHref || '/');
    } else if (action === 'landing-nav-toggle') {
      const nav = element.parentElement?.querySelector('nav');
      const open = nav?.classList.toggle('is-open') || false;
      element.setAttribute('aria-expanded', open ? 'true' : 'false');
    } else if (action === 'landing-search-focus') {
      if (event.target === element) document.getElementById('landingSearch')?.focus();
    } else if (action === 'settings-team') {
      window.toggleSettingsPanel?.(false);
      window.quickAction?.('seller_team', {});
    } else if (action === 'settings-invite') {
      window.toggleSettingsPanel?.(false);
      window.quickAction?.('invite_customer', {});
    } else if (action === 'quick-action') {
      let params = {};
      try { params = JSON.parse(element.dataset.uiParams || '{}'); } catch (_) { params = {}; }
      window.quickAction?.(element.dataset.uiName || '', params);
    } else if (action === 'assistant-suggest') {
      window.aiAsk?.(element.textContent || '');
    } else if (action === 'assistant-feedback') {
      window.aiFb?.(element.dataset.messageId, Number(element.dataset.feedback), element);
    } else if (action === 'close-side-preview') {
      window.closeSidePreview?.();
    } else if (action === 'clear-project-history') {
      window.__clearHistory?.();
    } else {
      return false;
    }
    return true;
  }

  document.addEventListener('click', function (event) {
    const element = event.target.closest('[data-ui-click],[data-ui-action]');
    if (!element) return;
    if (handleSpecialClick(element, event)) return;
    callDeclared(element, 'click', event);
  });

  for (const eventName of ['input', 'change', 'keydown']) {
    document.addEventListener(eventName, function (event) {
      const element = event.target.closest(`[data-ui-${eventName}]`);
      if (element) callDeclared(element, eventName, event);
    });
  }

  document.addEventListener('submit', function (event) {
    const form = event.target.closest('[data-ui-sync-csrf]');
    if (!form) return;
    const match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    if (match && form.csrfmiddlewaretoken) {
      form.csrfmiddlewaretoken.value = decodeURIComponent(match[1]);
    }
  });
})();
