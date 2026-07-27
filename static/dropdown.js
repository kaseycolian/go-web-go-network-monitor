/* Accessible custom dropdown — progressive enhancement of <select data-drop>.
   Pattern: select-only combobox (WAI-ARIA). The native <select> stays in the
   DOM (display:none) as the value store, so existing `.onchange` handlers and
   `.value` reads keep working; the custom UI dispatches a native 'change' on
   pick. Keyboard: Up/Down/Home/End move, Enter/Space select, Esc close, Tab
   closes, type-ahead jumps. Click-outside closes. Works with touch.

   API:  WtbDrop.enhanceAll(root?)   -> enhance every [data-drop] select
         WtbDrop.enhance(select)     -> enhance one (idempotent) -> returns api
         select._drop.rebuild()      -> re-read options (after they change)
         select._drop.sync()         -> re-read the current value (after value=) */
(function () {
  "use strict";
  var open = null;                 // currently-open instance
  var uid = 0;

  function enhance(select) {
    if (select._drop) return select._drop;
    var id = select.id || ("drop" + (++uid));
    var label = select.getAttribute("aria-label") || "";

    var wrap = document.createElement("div");
    wrap.className = "drop";

    // a div (not a button) so Space/Enter don't synthesize a second click
    var toggle = document.createElement("div");
    toggle.className = "drop-toggle";
    toggle.tabIndex = 0;
    toggle.setAttribute("role", "combobox");
    toggle.setAttribute("aria-haspopup", "listbox");
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-controls", id + "-panel");
    if (label) toggle.setAttribute("aria-label", label);

    var value = document.createElement("span");
    value.className = "drop-value";
    var caret = document.createElement("span");
    caret.className = "caret"; caret.setAttribute("aria-hidden", "true");
    caret.textContent = "▾";
    toggle.appendChild(value); toggle.appendChild(caret);

    var panel = document.createElement("div");
    panel.className = "drop-panel"; panel.id = id + "-panel";
    panel.setAttribute("role", "listbox");
    if (label) panel.setAttribute("aria-label", label);
    panel.hidden = true;

    // place wrap where the select is, then move the select inside (hidden store)
    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(toggle); wrap.appendChild(panel); wrap.appendChild(select);
    select.classList.add("drop-native");
    select.setAttribute("aria-hidden", "true");
    select.tabIndex = -1;

    var rows = [];
    var active = -1;

    function rebuild() {
      panel.innerHTML = "";
      rows = [];
      Array.prototype.forEach.call(select.options, function (opt, i) {
        var row = document.createElement("div");
        row.className = "drop-row";
        row.id = id + "-o" + i;
        row.setAttribute("role", "option");
        row.dataset.i = i;
        row.textContent = opt.textContent;
        row.setAttribute("aria-selected", i === select.selectedIndex
          ? "true" : "false");
        row.addEventListener("click", function () { choose(i); });
        row.addEventListener("pointermove", function () { setActive(i, false); });
        panel.appendChild(row);
        rows.push(row);
      });
      if (!rows.length) {
        var e = document.createElement("div");
        e.className = "drop-empty"; e.textContent = "No options";
        panel.appendChild(e);
      }
      syncLabel();
    }

    function syncLabel() {
      var o = select.options[select.selectedIndex];
      value.textContent = o ? o.textContent : "";
      rows.forEach(function (r) {
        r.setAttribute("aria-selected",
          +r.dataset.i === select.selectedIndex ? "true" : "false");
      });
    }

    function setActive(i, scroll) {
      if (i < 0 || i >= rows.length) return;
      active = i;
      rows.forEach(function (r, j) { r.classList.toggle("active", j === i); });
      toggle.setAttribute("aria-activedescendant", rows[i].id);
      if (scroll !== false) rows[i].scrollIntoView({ block: "nearest" });
    }

    function openPanel() {
      if (!panel.hidden) return;
      if (open && open !== api) open.close();
      // flip up if there isn't room below
      var r = toggle.getBoundingClientRect();
      var below = window.innerHeight - r.bottom;
      wrap.classList.toggle("drop-up", below < 280 && r.top > below);
      panel.hidden = false;
      toggle.setAttribute("aria-expanded", "true");
      open = api;
      setActive(select.selectedIndex < 0 ? 0 : select.selectedIndex, true);
      document.addEventListener("pointerdown", onDocDown, true);
    }
    function close() {
      if (panel.hidden) return;
      panel.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
      toggle.removeAttribute("aria-activedescendant");
      if (open === api) open = null;
      document.removeEventListener("pointerdown", onDocDown, true);
    }
    function onDocDown(e) { if (!wrap.contains(e.target)) close(); }

    function choose(i) {
      if (i < 0 || i >= select.options.length) return;
      if (select.selectedIndex !== i) {
        select.selectedIndex = i;
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
      syncLabel();
      close();
      toggle.focus();
    }

    var buf = "", bufT = 0;
    function typeahead(ch) {
      var now = Date.now();
      buf = (now - bufT > 800 ? "" : buf) + ch.toLowerCase();
      bufT = now;
      for (var j = 0; j < select.options.length; j++) {
        if (select.options[j].textContent.toLowerCase().indexOf(buf) === 0) {
          panel.hidden ? choose(j) : setActive(j);
          return;
        }
      }
    }

    toggle.addEventListener("click", function () {
      panel.hidden ? openPanel() : close();
    });
    toggle.addEventListener("keydown", function (e) {
      var k = e.key;
      if (panel.hidden) {
        if (k === "ArrowDown" || k === "ArrowUp" || k === "Enter" || k === " ") {
          e.preventDefault(); openPanel(); return;
        }
      } else {
        if (k === "ArrowDown") { e.preventDefault(); setActive(Math.min(rows.length - 1, active + 1)); return; }
        if (k === "ArrowUp") { e.preventDefault(); setActive(Math.max(0, active - 1)); return; }
        if (k === "Home") { e.preventDefault(); setActive(0); return; }
        if (k === "End") { e.preventDefault(); setActive(rows.length - 1); return; }
        if (k === "Enter" || k === " ") { e.preventDefault(); choose(active); return; }
        if (k === "Escape") { e.preventDefault(); e.stopPropagation(); close(); return; }
        if (k === "Tab") { close(); return; }
      }
      if (k.length === 1 && /\S/.test(k)) { e.preventDefault(); typeahead(k); }
    });

    // mirror the native select's `hidden` attribute onto the wrap, so app code
    // that toggles select.hidden (e.g. the peer switcher) just works
    function mirrorHidden() { wrap.hidden = select.hidden; }
    mirrorHidden();
    new MutationObserver(mirrorHidden)
      .observe(select, { attributes: true, attributeFilter: ["hidden"] });
    // keep the label in sync if something dispatches change on the native
    select.addEventListener("change", syncLabel);

    var api = { rebuild: rebuild, sync: syncLabel, close: close, wrap: wrap };
    select._drop = api;
    rebuild();
    return api;
  }

  function enhanceAll(root) {
    (root || document).querySelectorAll("select[data-drop]").forEach(enhance);
  }

  window.WtbDrop = { enhance: enhance, enhanceAll: enhanceAll };
})();
