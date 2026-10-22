// Cebelihle chat widget. Each question is sent on its own; the server answers from the portal's knowledge base and,
// for questions about "my" records, from whoever is signed in. The visible conversation is kept for this tab only.
(function () {
    var root = document.querySelector('[data-chatbot]');
    if (!root) { return; }
    var panel = root.querySelector('.cb-panel');
    var launcher = root.querySelector('[data-chat-open]');
    var log = root.querySelector('[data-chat-log]');
    var form = root.querySelector('[data-chat-form]');
    var input = root.querySelector('[data-chat-input]');
    var storeKey = 'cebelihle:' + root.dataset.chatUrl;
    var history = [];
    var busy = false;

    function load() {
        try { history = JSON.parse(sessionStorage.getItem(storeKey) || '[]') || []; } catch (e) { history = []; }
    }
    function save() {
        try { sessionStorage.setItem(storeKey, JSON.stringify(history.slice(-30))); } catch (e) { /* storage unavailable */ }
    }

    // Escape everything, then allow **bold**, "- " bullets, line breaks and links to portal paths.
    function format(text) {
        var html = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        html = html.replace(/(^|[\s(])(\/[a-z][a-z0-9\-\/_]*[a-z0-9])/gi, function (match, lead, path) {
            return lead + '<a href="' + path + '">' + path + '</a>';
        });
        var out = [], inList = false;
        html.split('\n').forEach(function (line) {
            var bullet = /^\s*[-•]\s+(.*)$/.exec(line);
            if (bullet) {
                if (!inList) { out.push('<ul>'); inList = true; }
                out.push('<li>' + bullet[1] + '</li>');
                return;
            }
            if (inList) { out.push('</ul>'); inList = false; }
            if (line.trim()) { out.push('<p>' + line + '</p>'); }
        });
        if (inList) { out.push('</ul>'); }
        return out.join('');
    }

    function addMessage(role, text, extraClass) {
        var div = document.createElement('div');
        div.className = 'cb-msg cb-msg--' + (role === 'user' ? 'user' : 'bot') + (extraClass ? ' ' + extraClass : '');
        if (role === 'user') {
            div.textContent = text;
            log.appendChild(div);
        } else {
            div.innerHTML = format(text);
            var row = document.createElement('div');
            row.className = 'cb-row';
            var avatar = document.createElement('img');
            avatar.className = 'cb-msg-avatar';
            avatar.src = root.dataset.avatar;
            avatar.alt = '';
            row.appendChild(avatar);
            row.appendChild(div);
            log.appendChild(row);
        }
        log.scrollTop = log.scrollHeight;
        return div;
    }

    function addChips(suggestions) {
        if (!suggestions || !suggestions.length) { return; }
        var wrap = document.createElement('div');
        wrap.className = 'cb-chips';
        suggestions.forEach(function (text) {
            var chip = document.createElement('button');
            chip.type = 'button';
            chip.className = 'cb-chip';
            chip.textContent = text;
            wrap.appendChild(chip);
        });
        log.appendChild(wrap);
        log.scrollTop = log.scrollHeight;
    }

    function render() {
        log.innerHTML = '';
        history.forEach(function (turn) { addMessage(turn.role, turn.content); });
    }

    function setOpen(open) {
        panel.hidden = !open;
        launcher.setAttribute('aria-expanded', open ? 'true' : 'false');
        root.classList.toggle('is-open', open);
        try { sessionStorage.setItem(storeKey + ':open', open ? '1' : ''); } catch (e) { /* ignore */ }
        if (open) { input.focus(); log.scrollTop = log.scrollHeight; }
    }

    function send(text) {
        if (busy) { return; }
        busy = true;
        log.querySelectorAll('.cb-chips').forEach(function (el) { el.remove(); });
        history.push({ role: 'user', content: text });
        addMessage('user', text);
        fetch(root.dataset.chatUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': root.dataset.csrf },
            body: JSON.stringify({ message: text }),
            credentials: 'same-origin'
        }).then(function (resp) {
            return resp.json().catch(function () { return {}; });
        }).then(function (data) {
            var reply = data.reply || 'Sorry, something went wrong. Please try again.';
            history.push({ role: 'assistant', content: reply });
            save();
            addMessage('assistant', reply);
            addChips(data.suggestions);
        }).catch(function () {
            history.pop();
            save();
            addMessage('assistant', 'I couldn\'t reach the server. Check your connection and try again.', 'cb-msg--error');
        }).finally(function () {
            busy = false;
            input.focus();
        });
    }

    launcher.addEventListener('click', function () { setOpen(panel.hidden); });
    root.querySelector('[data-chat-close]').addEventListener('click', function () { setOpen(false); });
    root.querySelector('[data-chat-reset]').addEventListener('click', function () {
        history = [];
        save();
        render();
        input.focus();
    });
    log.addEventListener('click', function (e) {
        var chip = e.target.closest('.cb-chip');
        if (chip) { send(chip.textContent); }
    });
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && !panel.hidden) { setOpen(false); launcher.focus(); }
    });
    input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
    });
    input.addEventListener('input', function () {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    });
    form.addEventListener('submit', function (e) {
        e.preventDefault();
        var text = input.value.trim();
        if (!text || busy) { return; }
        input.value = '';
        input.style.height = 'auto';
        send(text);
    });

    load();
    render();
    var wasOpen = false;
    try { wasOpen = !!sessionStorage.getItem(storeKey + ':open'); } catch (e) { /* ignore */ }
    if (wasOpen) { setOpen(true); }
})();
