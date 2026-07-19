function isIos() {
  return /iphone|ipad|ipod/i.test(navigator.userAgent) && !window.MSStream;
}

function isAndroid() {
  return /android/i.test(navigator.userAgent);
}

function isStandalone() {
  return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
}

// The installed app (Add to Home Screen) is Replies-only — everything else
// stays reachable from the regular browser tab. This runs synchronously,
// before the rest of the page parses, so a restricted screen never actually
// renders when opened from the home-screen icon.
(function restrictInstalledAppToReplies() {
  const RESTRICTED_PATHS = ['/', '/templates', '/contacts', '/send'];
  if (isStandalone() && RESTRICTED_PATHS.includes(window.location.pathname)) {
    window.location.replace('/replies');
  }
})();

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

// The button is visible by default (see login.html) so there's always
// something to click. If the browser handed us a real install prompt, use
// it; otherwise fall back to plain-language instructions for the platform,
// since not every browser (notably iOS Safari) supports one-tap install.
async function installApp() {
  if (isStandalone()) {
    // Already running as the installed app — nothing to install. Defensive
    // guard in case the button was ever visible here by mistake.
    hideInstallButton();
    return;
  }

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
  // The service worker's push handler posts this so an already-open page can
  // refresh immediately instead of waiting for its next poll tick.
  navigator.serviceWorker.addEventListener('message', (event) => {
    if (event.data?.type === 'new-reply') {
      window.dispatchEvent(new CustomEvent('push-new-reply'));
    }
  });
}

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64  = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = atob(base64);
  return Uint8Array.from([...rawData].map((c) => c.charCodeAt(0)));
}

// Requests notification permission, subscribes via the Push API, and hands
// the subscription to the backend so it can send real OS-level notifications
// even when this app is fully closed.
async function enablePush() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    alert('Push notifications are not supported in this browser.');
    return false;
  }

  const permission = await Notification.requestPermission();
  if (permission !== 'granted') return false;

  try {
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      const keyRes = await fetch('/api/push/vapid-public-key');
      const { key } = await keyRes.json();
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(key),
      });
    }
    await fetch('/api/push/subscribe', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sub.toJSON()),
    });
    return true;
  } catch (e) {
    alert('Could not enable notifications: ' + e.message);
    return false;
  }
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
