// vShop main.js

// Usage progress bar color logic
function setProgressColor(bar, pct) {
  bar.classList.remove('bg-green-500', 'bg-yellow-500', 'bg-red-500');
  if (pct >= 90)      bar.classList.add('bg-red-500');
  else if (pct >= 65) bar.classList.add('bg-yellow-500');
  else                bar.classList.add('bg-green-500');
}

document.querySelectorAll('[data-usage-bar]').forEach(bar => {
  const pct = parseInt(bar.dataset.usageBar, 10);
  bar.style.width = pct + '%';
  setProgressColor(bar, pct);
});

// Countdown timers
document.querySelectorAll('[data-expires]').forEach(el => {
  const exp = new Date(el.dataset.expires);
  function tick() {
    const diff = exp - Date.now();
    if (diff <= 0) { el.textContent = 'Expired'; el.classList.add('text-red-400'); return; }
    const d = Math.floor(diff / 86400000);
    const h = Math.floor((diff % 86400000) / 3600000);
    const m = Math.floor((diff % 3600000) / 60000);
    el.textContent = `${d}d ${h}h ${m}m`;
  }
  tick();
  setInterval(tick, 60000);
});

// Toggle password visibility
document.querySelectorAll('[data-toggle-password]').forEach(btn => {
  const target = document.getElementById(btn.dataset.togglePassword);
  btn.addEventListener('click', () => {
    const isPass = target.type === 'password';
    target.type = isPass ? 'text' : 'password';
    btn.querySelector('i').className = isPass ? 'fa-solid fa-eye-slash' : 'fa-solid fa-eye';
  });
});
