(function () {
  "use strict";

  var root = document.documentElement;
  var storedTheme = "";
  try {
    storedTheme = window.localStorage.getItem("cp-control-theme") || "";
  } catch (error) {
    storedTheme = "";
  }
  if (storedTheme === "dark" || storedTheme === "light") {
    root.dataset.controlTheme = storedTheme;
  } else if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
    root.dataset.controlTheme = "dark";
  }

  function closeSidebar() {
    document.body.classList.remove("is-menu-open");
  }

  document.querySelectorAll("[data-sidebar-toggle]").forEach(function (button) {
    button.addEventListener("click", function () {
      document.body.classList.toggle("is-menu-open");
    });
  });
  document.querySelectorAll("[data-sidebar-backdrop]").forEach(function (button) {
    button.addEventListener("click", closeSidebar);
  });
  document.querySelectorAll(".control-nav-link").forEach(function (link) {
    link.addEventListener("click", closeSidebar);
  });

  document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
    button.addEventListener("click", function () {
      var theme = root.dataset.controlTheme === "dark" ? "light" : "dark";
      root.dataset.controlTheme = theme;
      try {
        window.localStorage.setItem("cp-control-theme", theme);
      } catch (error) {
        return;
      }
    });
  });

  document.querySelectorAll("[data-message-close]").forEach(function (button) {
    button.addEventListener("click", function () {
      var message = button.closest(".control-message");
      if (message) message.remove();
    });
  });

  document.querySelectorAll("[data-reveal-form]").forEach(function (button) {
    button.addEventListener("click", function () {
      var form = document.getElementById(button.dataset.revealForm);
      if (!form) return;
      form.hidden = !form.hidden;
      if (!form.hidden) {
        var field = form.querySelector("input:not([type='hidden'])");
        if (field) field.focus();
      }
    });
  });

  document.addEventListener("keydown", function (event) {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      var search = document.querySelector(".control-global-search input");
      if (!search) return;
      event.preventDefault();
      search.focus();
      search.select();
    }
    if (event.key === "Escape") closeSidebar();
  });
})();
