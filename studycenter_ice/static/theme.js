// Browser-local appearance only; never changes account or study data.
(() => {
  const key = 'studycenter_ice.theme';
  const modes = new Set(['light', 'dark', 'system']);
  const system = window.matchMedia('(prefers-color-scheme: dark)');
  let mode = 'light';
  try { const saved = localStorage.getItem(key); if (modes.has(saved)) mode = saved; } catch (_) {}
  function apply() {
    document.documentElement.dataset.theme = mode === 'system' ? (system.matches ? 'dark' : 'light') : mode;
    document.querySelectorAll('[data-theme-choice]').forEach(input => { input.checked = input.value === mode; });
  }
  apply();
  system.addEventListener('change', () => { if (mode === 'system') apply(); });
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    document.querySelectorAll('[data-theme-choice]').forEach(input => input.addEventListener('change', () => {
      if (!input.checked || !modes.has(input.value)) return;
      mode = input.value;
      try { localStorage.setItem(key, mode); } catch (_) {}
      apply();
    }));
  });
})();
