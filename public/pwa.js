// Capture the install prompt as early as possible (this script must load
// early in <head>, without `defer`) — Chrome can fire `beforeinstallprompt`
// before a deferred/bottom-of-page script would even run, and the event is
// only delivered to listeners already attached at that exact moment.
window.__deferredInstallPrompt = null;

function hideInstallButton() {
  document.querySelectorAll('.js-install-app').forEach((btn) => {
    btn.style.display = 'none';
  });
}

window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  window.__deferredInstallPrompt = e;
});

window.addEventListener('appinstalled', () => {
  window.__deferredInstallPrompt = null;
  hideInstallButton();
});

function isIos() {
  return /iphone|ipad|ipod/i.test(navigator.userAgent) && !window.MSStream;
}

function isAndroid() {
  return /android/i.test(navigator.userAgent);
}

function isStandalone() {
  return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
}

// The button is visible by default (see login.html) so there's always
// something to click. If the browser handed us a real install prompt, use
// it; otherwise fall back to plain-language instructions for the platform,
// since not every browser (notably iOS Safari) supports one-tap install.
async function installApp() {
  const promptEvent = window.__deferredInstallPrompt;
  if (promptEvent) {
    promptEvent.prompt();
    await promptEvent.userChoice;
    window.__deferredInstallPrompt = null;
    hideInstallButton();
    return;
  }

  if (isIos()) {
    alert('On iPhone/iPad:\n\n1. Tap the Share icon (square with an arrow)\n2. Tap "Add to Home Screen"');
  } else if (isAndroid()) {
    alert('Tap the ⋮ menu (top-right of your browser) → "Add to Home screen" or "Install app".');
  } else {
    alert('Look for an install icon in your browser\'s address bar (often on the right side), or open the browser menu and choose "Install [app name]…".');
  }
}

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  });
}

document.addEventListener('DOMContentLoaded', () => {
  if (isStandalone()) {
    hideInstallButton(); // already installed/running as an app — nothing to install
    return;
  }
  if (isIos()) {
    document.querySelectorAll('.js-install-ios-hint').forEach((el) => { el.style.display = ''; });
  }
});
