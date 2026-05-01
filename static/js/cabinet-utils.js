/* Shared cabinet utilities: search debounce, empty state injection, etc. */
(function(){
  'use strict';

  // ── Search debounce ──
  // Usage: <input data-search="rows-container" data-search-attr="data-search-text" placeholder="...">
  // Will filter <tr data-search-text="..."> children of #rows-container by typed text.
  function setupSearchDebounce() {
    document.querySelectorAll('input[data-search]').forEach(function(inp){
      var targetId = inp.dataset.search;
      var attr = inp.dataset.searchAttr || 'data-search-text';
      var timer = null;
      inp.addEventListener('input', function(){
        clearTimeout(timer);
        timer = setTimeout(function(){
          var q = inp.value.trim().toLowerCase();
          var container = document.getElementById(targetId);
          if (!container) return;
          var rows = container.querySelectorAll('[' + attr + ']');
          var visible = 0;
          rows.forEach(function(row){
            var text = (row.getAttribute(attr) || '').toLowerCase();
            var match = !q || text.indexOf(q) !== -1;
            row.style.display = match ? '' : 'none';
            if (match) visible++;
          });
          // Show empty state if no rows visible
          var empty = container.querySelector('[data-search-empty]');
          if (empty) empty.style.display = (visible === 0 && q) ? '' : 'none';
        }, 250);
      });
    });
  }

  // ── Auto-attach skeleton loader on async fetch buttons ──
  // Usage: <button data-loader="rows-container">Refresh</button>
  function setupLoaders() {
    document.querySelectorAll('[data-loader]').forEach(function(btn){
      btn.addEventListener('click', function(){
        var container = document.getElementById(btn.dataset.loader);
        if (!container) return;
        container.style.opacity = '0.4';
        container.style.pointerEvents = 'none';
        setTimeout(function(){
          container.style.opacity = '';
          container.style.pointerEvents = '';
        }, 800);
      });
    });
  }

  // ── Toast notification ──
  window.cpToast = function(message, type, duration) {
    type = type || 'info';
    duration = duration || 2500;
    var c = document.getElementById('cp-toast-container');
    if (!c) {
      c = document.createElement('div');
      c.id = 'cp-toast-container';
      c.style.cssText = 'position:fixed;top:20px;right:20px;z-index:10000;display:flex;flex-direction:column;gap:8px;';
      document.body.appendChild(c);
    }
    var t = document.createElement('div');
    var bg = {success:'rgba(102,187,106,0.95)', warn:'rgba(232,92,13,0.95)', error:'rgba(232,92,13,0.95)', info:'#2C2C2C'}[type];
    t.style.cssText = 'background:'+bg+';color:#fff;padding:12px 18px;border-radius:10px;font-size:13px;font-family:Inter,sans-serif;box-shadow:0 4px 16px rgba(0,0,0,0.3);max-width:340px;animation:cpToastIn .25s ease-out;';
    t.textContent = message;
    c.appendChild(t);
    setTimeout(function(){ t.style.opacity='0'; t.style.transform='translateY(-12px)'; t.style.transition='all .2s'; }, duration - 200);
    setTimeout(function(){ t.remove(); }, duration);
  };

  document.addEventListener('DOMContentLoaded', function(){
    setupSearchDebounce();
    setupLoaders();
  });
})();
