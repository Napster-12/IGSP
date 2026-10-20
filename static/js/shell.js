// Shared behaviour for the signed-in layouts (supplier_base.html and admin_base.html).
(function () {
    // Phone / tablet menu: the sidebar collapses into a top bar with a Menu button.
    var toggle = document.querySelector('[data-menu-toggle]');
    var menu = toggle && document.getElementById(toggle.getAttribute('aria-controls'));
    if (toggle && menu) {
        function setOpen(open) {
            document.body.classList.toggle('ad-menu-open', open);
            toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        }
        toggle.addEventListener('click', function () {
            setOpen(!document.body.classList.contains('ad-menu-open'));
        });
        menu.addEventListener('click', function (event) {
            if (event.target.closest('a')) { setOpen(false); }
        });
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape' && document.body.classList.contains('ad-menu-open')) {
                setOpen(false);
                toggle.focus();
            }
        });
        document.addEventListener('click', function (event) {
            if (document.body.classList.contains('ad-menu-open') && !event.target.closest('.ad-sidebar')) { setOpen(false); }
        });
    }

    // On phones, table rows are shown as cards: label each cell with its column heading.
    document.querySelectorAll('table.ad-table:not(.directors-table)').forEach(function (table) {
        var headings = Array.prototype.map.call(table.querySelectorAll('thead th'), function (th) {
            return th.querySelector('.ad-sr') ? '' : th.textContent.trim();
        });
        table.querySelectorAll('tbody tr').forEach(function (row) {
            if (row.cells.length !== headings.length) { return; }  // e.g. a full-width "no results" row
            Array.prototype.forEach.call(row.cells, function (cell, i) {
                if (!cell.hasAttribute('data-label')) { cell.setAttribute('data-label', headings[i]); }
            });
        });
        table.classList.add('ad-table--stack');
    });
})();
