(function () {
  "use strict";

  const storageKey = "lead-studio-theme";
  const root = document.documentElement;
  const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");

  function savedTheme() {
    try {
      const value = localStorage.getItem(storageKey);
      return value === "light" || value === "dark" ? value : null;
    } catch {
      return null;
    }
  }

  function updateControls(theme) {
    const next = theme === "dark" ? "light" : "dark";
    document.querySelectorAll(".theme-toggle").forEach((button) => {
      button.setAttribute("aria-label", `Switch to ${next} mode`);
      button.title = `Switch to ${next} mode`;
      const icon = button.querySelector(".theme-icon");
      if (icon) icon.textContent = theme === "dark" ? "☀" : "☾";
    });
    const themeMeta = document.querySelector('meta[name="theme-color"]');
    if (themeMeta) themeMeta.content = theme === "dark" ? "#0b1120" : "#f4f6fb";
  }

  function applyTheme(theme, persist) {
    root.dataset.theme = theme;
    root.style.colorScheme = theme;
    if (persist) {
      try { localStorage.setItem(storageKey, theme); } catch { /* storage may be disabled */ }
    }
    updateControls(theme);
  }

  applyTheme(savedTheme() || (systemTheme.matches ? "dark" : "light"), false);

  document.addEventListener("DOMContentLoaded", () => {
    updateControls(root.dataset.theme);
    document.querySelectorAll(".theme-toggle").forEach((button) => {
      button.addEventListener("click", () => {
        applyTheme(root.dataset.theme === "dark" ? "light" : "dark", true);
      });
    });
  });

  systemTheme.addEventListener?.("change", (event) => {
    if (!savedTheme()) applyTheme(event.matches ? "dark" : "light", false);
  });
}());
