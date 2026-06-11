/* vShop – global JS helpers
   Toast, clipboard, and confirm utilities are already inlined in base.html.
   Add page-specific helpers here if needed. */

// Auto-uppercase reference code inputs
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('input[data-uppercase]').forEach(el => {
    el.addEventListener('input', () => { el.value = el.value.toUpperCase(); });
  });
});
