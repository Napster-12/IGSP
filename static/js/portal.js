// Shared behaviour for the supplier forms (onboarding steps and edit application).
(function () {
    // Category radios: show the "please specify" box only for the selected category.
    function syncCategoryDetails() {
        document.querySelectorAll('[data-detail-for]').forEach(function (box) {
            var radio = document.querySelector('input[name="categories"][data-index="' + box.dataset.detailFor + '"]');
            var show = !!(radio && radio.checked);
            box.hidden = !show;
            var input = box.querySelector('input');
            if (input) { input.required = show; }
        });
        // Bottom-bar summary on the category step.
        var summary = document.querySelector('[data-category-summary]');
        if (summary) {
            var checked = document.querySelector('input[name="categories"]:checked');
            summary.innerHTML = '';
            if (checked) {
                summary.appendChild(document.createTextNode('Selected: '));
                var strong = document.createElement('strong');
                strong.textContent = checked.dataset.title || checked.value;
                summary.appendChild(strong);
            } else {
                summary.textContent = 'No category selected';
            }
        }
    }

    // File inputs: show the chosen file name in the slot.
    function showFileName(input) {
        var slot = input.closest('.ad-slot');
        var label = slot && slot.querySelector('[data-file-name]');
        if (!label || !input.files || !input.files[0]) { return; }
        label.textContent = 'Selected: ' + input.files[0].name + ' (uploads when you continue)';
        slot.classList.add('is-selected');
    }

    var nextDirectorIndex = 0;
    function addDirectorRow() {
        var template = document.getElementById('director-row-template');
        var tbody = document.querySelector('#directors-table tbody');
        if (!template || !tbody) { return; }
        tbody.insertAdjacentHTML('beforeend', template.innerHTML.replace(/\{INDEX\}/g, nextDirectorIndex++));
        var row = tbody.lastElementChild;
        var first = row && row.querySelector('input');
        if (first) { first.focus(); }
    }

    document.addEventListener('change', function (e) {
        if (e.target.matches('input[name="categories"]')) { syncCategoryDetails(); }
        if (e.target.matches('[data-file-input]')) { showFileName(e.target); }
    });

    document.addEventListener('click', function (e) {
        if (e.target.closest('[data-add-director]')) {
            e.preventDefault();
            addDirectorRow();
        }
        var remove = e.target.closest('[data-remove-row]');
        if (remove) {
            var rows = document.querySelectorAll('#directors-table tbody tr');
            if (rows.length > 1) {
                remove.closest('tr').remove();
            } else {
                // Keep one row so the table never disappears; just clear it.
                remove.closest('tr').querySelectorAll('input').forEach(function (i) { i.value = ''; });
            }
        }
    });

    document.addEventListener('DOMContentLoaded', function () {
        syncCategoryDetails();
        // Server-rendered rows use 0..n-1; new rows continue after the highest index.
        document.querySelectorAll('#directors-table [name^="director_initials_"]').forEach(function (input) {
            var n = parseInt(input.name.replace('director_initials_', ''), 10);
            if (!isNaN(n) && n >= nextDirectorIndex) { nextDirectorIndex = n + 1; }
        });
    });
})();
