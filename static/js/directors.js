// Live validation for director/shareholder rows. Mirrors the server rules in app.py:
// South Africans need a valid 13-digit SA ID; everyone else a 6-20 character passport number.
(function () {
    function luhnValid(digits) {
        var total = 0;
        for (var i = 0; i < digits.length; i++) {
            var n = parseInt(digits.charAt(digits.length - 1 - i), 10);
            if (i % 2 === 1) { n *= 2; if (n > 9) { n -= 9; } }
            total += n;
        }
        return total % 10 === 0;
    }

    function validDate(yy, mm, dd) {
        return [1900, 2000].some(function (century) {
            var d = new Date(century + yy, mm - 1, dd);
            return d.getFullYear() === century + yy && d.getMonth() === mm - 1 && d.getDate() === dd;
        });
    }

    function idError(value, nationality) {
        var id = value.replace(/[\s-]/g, '').toUpperCase();
        if (!id) { return ''; }
        if (nationality === 'South Africa') {
            if (!/^\d{13}$/.test(id)) { return 'SA ID number must be exactly 13 digits (' + id.length + ' entered).'; }
            if (!validDate(+id.slice(0, 2), +id.slice(2, 4), +id.slice(4, 6))) { return 'First 6 digits must be a valid date of birth (YYMMDD).'; }
            if ('012'.indexOf(id.charAt(10)) === -1) { return 'Invalid citizenship digit (11th digit).'; }
            if (!luhnValid(id)) { return 'This is not a valid SA ID number. Please check for typos.'; }
            return '';
        }
        if (!/^[A-Z0-9]{6,20}$/.test(id)) { return 'Passport number must be 6–20 letters or digits.'; }
        return '';
    }

    function validateRow(row) {
        var idInput = row.querySelector('.director-id');
        var select = row.querySelector('.director-nationality');
        if (!idInput || !select) { return true; }
        var isSA = select.value === 'South Africa';
        idInput.placeholder = isSA ? '13-digit SA ID number' : 'Passport number';
        idInput.inputMode = isSA ? 'numeric' : 'text';
        var message = idError(idInput.value, select.value);
        idInput.setCustomValidity(message);
        var hint = row.querySelector('.director-id-hint');
        if (hint) { hint.textContent = message; }
        return !message;
    }

    function rowsIn(root) { return root.querySelectorAll('#directors-table tbody tr'); }

    document.addEventListener('input', function (e) {
        if (e.target.matches('.director-id, .director-nationality')) { validateRow(e.target.closest('tr')); }
    });
    document.addEventListener('change', function (e) {
        if (e.target.matches('.director-id, .director-nationality')) { validateRow(e.target.closest('tr')); }
    });
    document.addEventListener('DOMContentLoaded', function () {
        Array.prototype.forEach.call(rowsIn(document), validateRow);
        var table = document.getElementById('directors-table');
        var form = table && table.closest('form');
        if (form) {
            form.addEventListener('submit', function (e) {
                var ok = Array.prototype.every.call(rowsIn(document), validateRow);
                if (!ok) { e.preventDefault(); form.reportValidity(); }
            });
        }
    });
})();
