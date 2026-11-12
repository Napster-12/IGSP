// Shared behaviour for the signed-in layouts (supplier_base.html and admin_base.html).
(function () {
    // Older Safari (before 15.4) has no CSS :has(), so the states the styles need are set as classes here.
    if (document.querySelector('.ig-form-actions--sticky')) { document.body.classList.add('has-sticky-actions'); }
    document.querySelectorAll('.ig-slot').forEach(function (slot) {
        if (slot.querySelector('.ig-slot-dates')) { slot.classList.add('has-dates'); }
    });
    function syncChecked() {
        document.querySelectorAll('.cat-tile, .ig-choice').forEach(function (tile) {
            tile.classList.toggle('is-checked', !!tile.querySelector('input:checked'));
        });
    }
    syncChecked();
    document.addEventListener('change', syncChecked);
    document.addEventListener('focusin', function (e) {
        var tile = e.target.closest && e.target.closest('.cat-tile');
        var keyboard = true;
        try { keyboard = e.target.matches(':focus-visible'); } catch (err) { /* older browsers: assume keyboard */ }
        if (tile && keyboard) { tile.classList.add('has-focus'); }
    });
    document.addEventListener('focusout', function (e) {
        var tile = e.target.closest && e.target.closest('.cat-tile');
        if (tile) { tile.classList.remove('has-focus'); }
    });

    // Phone / tablet menu: the sidebar collapses into a top bar with a Menu button.
    var toggle = document.querySelector('[data-menu-toggle]');
    var menu = toggle && document.getElementById(toggle.getAttribute('aria-controls'));
    if (toggle && menu) {
        function setOpen(open) {
            document.body.classList.toggle('ig-menu-open', open);
            toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        }
        toggle.addEventListener('click', function () {
            setOpen(!document.body.classList.contains('ig-menu-open'));
        });
        menu.addEventListener('click', function (event) {
            if (event.target.closest('a')) { setOpen(false); }
        });
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape' && document.body.classList.contains('ig-menu-open')) {
                setOpen(false);
                toggle.focus();
            }
        });
        document.addEventListener('click', function (event) {
            if (document.body.classList.contains('ig-menu-open') && !event.target.closest('.ig-sidebar')) { setOpen(false); }
        });
    }

    // On phones, table rows are shown as cards: label each cell with its column heading.
    document.querySelectorAll('table.ig-table:not(.directors-table)').forEach(function (table) {
        var headings = Array.prototype.map.call(table.querySelectorAll('thead th'), function (th) {
            return th.querySelector('.ig-sr') ? '' : th.textContent.trim();
        });
        table.querySelectorAll('tbody tr').forEach(function (row) {
            if (row.cells.length !== headings.length) { return; }  // e.g. a full-width "no results" row
            Array.prototype.forEach.call(row.cells, function (cell, i) {
                if (!cell.hasAttribute('data-label')) { cell.setAttribute('data-label', headings[i]); }
            });
        });
        table.classList.add('ig-table--stack');
    });

    // Settings > Appearance: switch light/dark straight away and save it to the account in the background.
    var themeForm = document.querySelector('[data-theme-form]');
    if (themeForm) {
        var themeStatus = themeForm.querySelector('[data-theme-status]');
        var savedTheme = document.documentElement.getAttribute('data-theme') || 'light';
        themeForm.addEventListener('change', function (e) {
            if (e.target.name !== 'theme') { return; }
            var theme = e.target.value;
            var labelEl = e.target.closest('label');
            var label = labelEl ? labelEl.textContent.trim() : '';
            document.documentElement.setAttribute('data-theme', theme);
            themeForm.querySelectorAll('.ig-theme-option').forEach(function (opt) {
                opt.classList.toggle('is-checked', !!opt.querySelector('input:checked'));
            });
            themeStatus.textContent = 'Saving…';
            fetch(themeForm.action, { method: 'POST', body: new FormData(themeForm), credentials: 'same-origin',
                                      headers: { 'Accept': 'application/json' } })
                .then(function (r) { return r.json().then(function (data) { if (!r.ok || !data.ok) { throw data; } }); })
                .then(function () { savedTheme = theme; themeStatus.textContent = label + ' is on.'; })
                .catch(function () {
                    // Put the page back the way it was saved so it doesn't look switched when it isn't.
                    document.documentElement.setAttribute('data-theme', savedTheme);
                    var previous = themeForm.querySelector('input[name="theme"][value="' + savedTheme + '"]');
                    if (previous) { previous.checked = true; }
                    themeForm.querySelectorAll('.ig-theme-option').forEach(function (opt) {
                        opt.classList.toggle('is-checked', !!opt.querySelector('input:checked'));
                    });
                    themeStatus.textContent = "Couldn't save your choice. Please refresh the page and try again.";
                });
        });
    }
})();
