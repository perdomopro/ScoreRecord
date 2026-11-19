// ScoreRecord dashboard: theme toggle, dismissible notice, tooltips. No dependencies.
(function () {
  "use strict";
  var root = document.documentElement;

  function read(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
  function write(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* private mode */ } }

  // Theme: follows the OS until the viewer picks one.
  var toggle = document.querySelector("[data-theme-toggle]");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var dark = root.dataset.theme
        ? root.dataset.theme === "dark"
        : window.matchMedia("(prefers-color-scheme: dark)").matches;
      root.dataset.theme = dark ? "light" : "dark";
      write("sr-theme", root.dataset.theme);
    });
  }

  // One-line notice, shown until dismissed.
  var notice = document.querySelector("[data-notice]");
  if (notice && read("sr-notice") !== "1") {
    notice.hidden = false;
    notice.querySelector("[data-notice-close]").addEventListener("click", function () {
      notice.hidden = true;
      write("sr-notice", "1");
    });
  }

  // Keep the selected day visible in the horizontal date strip.
  var activeDay = document.querySelector(".day.is-active");
  if (activeDay && activeDay.parentElement) {
    var strip = activeDay.parentElement;
    strip.scrollLeft = activeDay.offsetLeft - strip.offsetLeft - (strip.clientWidth - activeDay.offsetWidth) / 2;
  }

  // Tooltips for any element with data-tip (chart marks, chips).
  var box = document.querySelector("[data-tip-box]");
  if (!box) return;
  var active = null;
  function place(x, y) {
    var w = box.offsetWidth, h = box.offsetHeight;
    var left = Math.min(Math.max(8, x + 12), window.innerWidth - w - 8);
    var top = y - h - 12 < 8 ? y + 16 : y - h - 12;
    box.style.left = left + "px";
    box.style.top = top + "px";
  }
  function show(target, x, y) {
    active = target;
    box.textContent = target.getAttribute("data-tip");
    box.hidden = false;
    place(x, y);
  }
  function hide() { active = null; box.hidden = true; }
  document.addEventListener("pointerover", function (e) {
    var t = e.target.closest && e.target.closest("[data-tip]");
    if (t) show(t, e.clientX, e.clientY);
  });
  document.addEventListener("pointermove", function (e) {
    if (active) place(e.clientX, e.clientY);
  });
  document.addEventListener("pointerout", function (e) {
    if (active && !(e.relatedTarget && active.contains(e.relatedTarget))) hide();
  });
  document.addEventListener("focusin", function (e) {
    var t = e.target.closest && e.target.closest("[data-tip]");
    if (t) { var r = t.getBoundingClientRect(); show(t, r.left + r.width / 2, r.top); }
  });
  document.addEventListener("focusout", hide);
  window.addEventListener("scroll", hide, { passive: true });
})();
