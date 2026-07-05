// IMPORTANT: this script must be loaded early in <head> WITHOUT `defer`, not
// at the bottom of <body>. Chrome can fire `beforeinstallprompt` very early —
// before a deferred/bottom-of-page script would even run — and the event is
// only delivered to listeners already attached at that exact moment. Miss it
// once and it never fires again for that page load, even though Chrome may
// still show its own install icon in the address bar.
window.__deferredInstallPrompt = null;

function showInstallButton() {
  document.querySelectorAll('.js-install-app').forEach((btn) => {
    btn.style.display = '';
  });
}

function hideInstallButton() {
  document.querySelectorAll('.js-install-app').forEach((btn) => {
    btn.style.display = 'none';
  });
}

window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  window.__deferredInstallPrompt = e;
  showInstallButton(); // no-op if the button isn't in the DOM yet — handled below
});

window.addEventListener('appinstalled', () => {
  window.__deferredInstallPrompt = null;
  hideInstallButton();
});

async function installApp() {
  const promptEvent = window.__deferredInstallPrompt;
  if (!promptEvent) return;
  promptEvent.prompt();
  await promptEvent.userChoice;
  window.__deferredInstallPrompt = null;
  hideInstallButton();
}

function isIos() {
  return /iphone|ipad|ipod/i.test(navigator.userAgent) && !window.MSStream;
}

function isStandalone() {
  return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
}

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  });
}

document.addEventListener('DOMContentLoaded', () => {
  if (isStandalone()) return; // already installed/running as an app
  if (window.__deferredInstallPrompt) showInstallButton(); // event fired before the button existed
  if (isIos()) {
    document.querySelectorAll('.js-install-ios-hint').forEach((el) => { el.style.display = ''; });
  }
});
