// Single source of truth for the app's left sidebar. Every tenant-facing
// page loads this with a plain synchronous <script src="/sidebar.js">
// placed exactly where the sidebar used to live in the HTML — that (not
// defer/async) matters: the browser pauses parsing right there, runs this,
// and the markup below lands in the DOM before the rest of the page renders,
// so there's no flash of a missing sidebar.
//
// Which link is "active" is detected from the current URL automatically —
// no per-page parameter to pass in and risk getting out of sync.
//
// admin.html has its own distinct sidebar (different sections entirely) and
// intentionally isn't part of this — it was never duplicated across
// multiple files the way this one was.
(function () {
  const NAV_ITEMS = [
    { section: 'Setup' },
    { href: '/',          icon: '⚡', label: 'WhatsApp Connect' },
    { section: 'Messaging' },
    { href: '/templates', icon: '📋', label: 'Templates' },
    { href: '/contacts',  icon: '👥', label: 'Contacts' },
    { href: '/send',      icon: '📤', label: 'Bulk Send' },
    { href: '/history',   icon: '📊', label: 'History' },
    { href: '/budget',    icon: '💰', label: 'Budget' },
    // Find Names is disabled — shown in the nav so it's visible, but
    // dimmed and unclickable (rendered as a <span>, not a link). The page
    // and its backend still work fine at /name-finder if linked to
    // directly; re-enable it by just removing `disabled: true` below.
    { href: '/name-finder', icon: '🔎', label: 'Find Names', disabled: true },
    { href: '/replies',   icon: '💬', label: 'Replies' },
  ];

  const path = window.location.pathname;
  const navHtml = NAV_ITEMS.map((item) => {
    if (item.section) return `<div class="nav-label">${item.section}</div>`;
    if (item.disabled) {
      return `<span class="nav-item disabled" title="Coming soon"><span class="nav-icon">${item.icon}</span> ${item.label}</span>`;
    }
    const active = item.href === path ? ' active' : '';
    return `<a href="${item.href}" class="nav-item${active}"><span class="nav-icon">${item.icon}</span> ${item.label}</a>`;
  }).join('');

  const html = `
    <aside class="sidebar">
      <div class="sidebar-brand">
        <div class="brand-icon"><img src="/icon-192.png" alt="V7 logo"></div>
        <div class="brand-text">
          <div class="app-name">V7</div>
          <div class="app-sub">Stress2Solutions</div>
        </div>
      </div>
      <nav class="sidebar-nav">${navHtml}</nav>
      <div class="sidebar-footer">
        <div class="conn-status">
          <span class="conn-dot" id="sidebar-dot"></span>
          <span id="sidebar-status-text">Checking...</span>
        </div>
        <div id="sidebar-user" style="margin-top:10px;display:none">
          <div style="font-size:11px;color:rgba(255,255,255,0.45);margin-bottom:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" id="sidebar-email"></div>
          <button onclick="doLogout()" style="width:100%;padding:7px;background:rgba(255,255,255,0.1);border:1px solid rgba(255,255,255,0.15);color:rgba(255,255,255,0.7);border-radius:7px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;transition:all 0.2s" onmouseover="this.style.background='rgba(255,255,255,0.18)'" onmouseout="this.style.background='rgba(255,255,255,0.1)'">
            🚪 Sign Out
          </button>
        </div>
      </div>
    </aside>`;

  document.currentScript.insertAdjacentHTML('beforebegin', html);
})();
