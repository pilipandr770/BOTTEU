/* BOTTEU — Main JavaScript */

// ── Cookie Banner ───────────────────────────────────────────────
(function () {
  const banner = document.getElementById('cookieBanner');
  if (banner && !localStorage.getItem('cookieConsent')) {
    banner.style.display = 'block';
  }
})();

function acceptCookies() {
  localStorage.setItem('cookieConsent', 'accepted');
  document.getElementById('cookieBanner').style.display = 'none';
}

function declineCookies() {
  localStorage.setItem('cookieConsent', 'declined');
  document.getElementById('cookieBanner').style.display = 'none';
}

// ── CSRF token for fetch requests ───────────────────────────────
function getCsrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  if (meta) return meta.content;
  const hidden = document.querySelector('input[name="csrf_token"]');
  return hidden ? hidden.value : '';
}

// ── Auto-dismiss alerts after 6s ────────────────────────────────
document.addEventListener('DOMContentLoaded', function () {
  setTimeout(function () {
    document.querySelectorAll('.alert.alert-dismissible').forEach(function (el) {
      const bsAlert = bootstrap.Alert.getOrCreateInstance(el);
      if (bsAlert) bsAlert.close();
    });
  }, 6000);
});
