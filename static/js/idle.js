// Signs the user out after a period without activity (the server enforces the same limit).
// Typing, clicking, scrolling and touching count as activity; while the user is active the server is told at most
// once a minute, so filling in a long form never counts as idle. A warning appears a minute before sign-out.
// Activity in any open portal tab keeps all of them signed in (shared through localStorage).
(function () {
    var config = document.querySelector('[data-idle-timeout]');
    if (!config) { return; }
    var TIMEOUT_MS = parseInt(config.dataset.idleTimeout, 10) * 1000;
    var WARN_MS = Math.min(60 * 1000, TIMEOUT_MS / 2);
    var PING_EVERY_MS = 60 * 1000;
    var KEY = 'igsp:lastActivity';
    var dialog = document.getElementById('idle-warning');
    var countdown = dialog && dialog.querySelector('[data-idle-countdown]');
    var lastPing = Date.now();
    var signingOut = false;

    function readShared() {
        try { return parseInt(localStorage.getItem(KEY), 10) || 0; } catch (e) { return 0; }
    }
    var lastActivity = Math.max(Date.now(), readShared());

    function ping() {
        lastPing = Date.now();
        fetch(config.dataset.keepaliveUrl, {
            method: 'POST',
            headers: { 'X-CSRFToken': config.dataset.csrf },
            credentials: 'same-origin'
        }).then(function (resp) {
            if (resp.status === 401) { signOut(); }
        }).catch(function () { /* offline: the server decides on the next request */ });
    }

    function activity() {
        if (signingOut) { return; }
        lastActivity = Date.now();
        try { localStorage.setItem(KEY, String(lastActivity)); } catch (e) { /* storage unavailable */ }
        if (dialog && !dialog.hidden) { dialog.hidden = true; }
        if (lastActivity - lastPing >= PING_EVERY_MS) { ping(); }
    }

    function signOut() {
        if (signingOut) { return; }
        signingOut = true;
        // On an application step, save what's been typed first (like "Save & continue later"); the server then
        // signs the supplier out.
        var form = document.querySelector('form[data-idle-save]');
        if (form) {
            [['save_later', '1'], ['idle_sign_out', '1']].forEach(function (pair) {
                var field = document.createElement('input');
                field.type = 'hidden';
                field.name = pair[0];
                field.value = pair[1];
                form.appendChild(field);
            });
            form.noValidate = true;
            HTMLFormElement.prototype.submit.call(form);
            return;
        }
        window.location.href = config.dataset.logoutUrl;
    }

    function tick() {
        lastActivity = Math.max(lastActivity, readShared());
        var left = TIMEOUT_MS - (Date.now() - lastActivity);
        if (left <= 0) { signOut(); return; }
        if (dialog && left <= WARN_MS) {
            dialog.hidden = false;
            if (countdown) { countdown.textContent = Math.ceil(left / 1000); }
        } else if (dialog && !dialog.hidden) {
            dialog.hidden = true;  // another tab was used
        }
    }

    ['keydown', 'mousedown', 'mousemove', 'wheel', 'scroll', 'touchstart', 'input'].forEach(function (name) {
        window.addEventListener(name, activity, { passive: true, capture: true });
    });
    if (dialog) {
        dialog.querySelector('[data-idle-stay]').addEventListener('click', function () {
            lastPing = 0;  // tell the server straight away
            activity();
        });
    }
    // After the laptop sleeps, timers pause: check as soon as the page is visible again.
    document.addEventListener('visibilitychange', function () { if (!document.hidden) { tick(); } });
    setInterval(tick, 1000);
})();
