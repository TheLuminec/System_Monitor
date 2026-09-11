// Restore the saved theme before first paint (external file: the CSP forbids inline scripts).
try { const t = localStorage.getItem("avm-theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) { /* ignore */ }
