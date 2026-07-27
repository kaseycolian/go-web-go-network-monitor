/* theme-service v0.3.0 — theme-init.js
   Applies the saved (or ?theme= / ?motion=) theme BEFORE first paint, so there's no flash.
   Load in <head> via <script src="theme/theme-init.js"></script> (NOT inline — inline is blocked
   by Manifest V3 / strict CSP). CSP-safe. */
(function () {
  try {
    var p = new URLSearchParams(location.search);
    var t = p.get('theme') || localStorage.getItem('theme');
    if (t) document.documentElement.setAttribute('data-theme', t);
    if ((p.get('motion') || localStorage.getItem('motion')) === 'off')
      document.documentElement.setAttribute('data-motion', 'off');
  } catch (e) {}
})();
