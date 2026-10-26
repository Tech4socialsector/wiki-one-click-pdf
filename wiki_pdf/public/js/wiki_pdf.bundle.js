// "Download PDF" button for the wiki, with a popup that says whether the PDF
// has the latest wiki changes.
//
// Plain JavaScript (no jQuery / frappe.ui), so it works on both:
// - Wiki 2 (Frappe 15): loaded via web_include_js, button in .navbar-nav
// - Wiki 3 (Frappe 16): added by wiki_pdf.web.add_assets_to_wiki_pages, whose
//   page layout has no jQuery or Frappe scripts; button in the header nav and
//   the mobile header.
(function () {
    var POLL_INTERVAL_MS = 10000;
    var MAX_WAIT_MS = 90 * 60 * 1000;
    var LABEL = 'Download PDF';

    function on_ready(fn) {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', fn);
        } else {
            fn();
        }
    }

    function escape_html(text) {
        var div = document.createElement('div');
        div.textContent = text == null ? '' : String(text);
        return div.innerHTML;
    }

    // "15 minutes ago" for a server datetime like "2026-10-06 14:11:33.81"
    function time_ago(value) {
        var then = new Date(String(value).replace(' ', 'T'));
        if (isNaN(then)) return '';
        var s = Math.max(0, (Date.now() - then.getTime()) / 1000);
        if (s < 60) return 'just now';
        var units = [['day', 86400], ['hour', 3600], ['minute', 60]];
        for (var i = 0; i < units.length; i++) {
            var n = Math.floor(s / units[i][1]);
            if (n >= 1) return n + ' ' + units[i][0] + (n > 1 ? 's' : '') + ' ago';
        }
        return '';
    }

    function get_selected_language() {
        var combo = document.querySelector('.goog-te-combo');
        if (combo && combo.value && combo.value !== '' && combo.value !== 'en') {
            return combo.value.split('-')[0];
        }
        var cookies = document.cookie.split(';');
        for (var i = 0; i < cookies.length; i++) {
            var c = cookies[i].trim();
            if (c.indexOf('googtrans=') === 0) {
                var parts = c.substring('googtrans='.length).trim().split('/');
                if (parts.length >= 3 && parts[2] && parts[2] !== 'en' && parts[2] !== 'auto') {
                    return parts[2];
                }
            }
        }
        return 'en';
    }

    // ── Button(s) ────────────────────────────────────────────────────────

    var buttons = [];

    function make_button(className, style) {
        var a = document.createElement('a');
        a.href = '#';
        a.className = 'wpdf-download-btn ' + className;
        if (style) a.setAttribute('style', style);
        a.textContent = LABEL;
        a.addEventListener('click', on_click);
        buttons.push(a);
        return a;
    }

    function add_buttons() {
        if (document.querySelector('.wpdf-download-btn')) return;

        // Wiki 2 (Frappe 15)
        var navbar = document.querySelector('.navbar-nav');
        if (navbar && document.querySelector('.wiki-content')) {
            var btn = make_button('navbar-link mr-4 d-print-none',
                'font-weight:600;cursor:pointer;color:var(--text-color);opacity:0.9;');
            // The theme toggle / search may sit deeper than the navbar itself.
            var target = navbar.querySelector('.sun-moon-container, .navbar-search');
            if (target) {
                target.parentNode.insertBefore(btn, target);
            } else {
                navbar.insertBefore(btn, navbar.firstChild);
            }
            return;
        }

        // Wiki 3 (Frappe 16). The desktop header differs between Wiki 3
        // releases (with or without a <nav>), but always has the theme toggle,
        // so the button goes just before it (and before the search button
        // next to it). The mobile header (hidden on desktop) is skipped here.
        if (!document.getElementById('wiki-content')) return;
        var toggles = document.querySelectorAll(
            'header button[title="Toggle theme"], header button[aria-label="Toggle theme"]'
        );
        for (var i = 0; i < toggles.length; i++) {
            var toggle = toggles[i];
            if (toggle.closest('[data-testid="mobile-header"], .lg\\:hidden')) continue;
            var before = toggle;
            var prev = toggle.previousElementSibling;
            if (prev && prev.tagName === 'BUTTON' && prev.querySelector('kbd')) before = prev;  // search
            before.parentNode.insertBefore(make_button(
                'px-1 py-1.5 text-sm-medium text-[var(--ink-gray-5)] hover:text-[var(--ink-gray-9)] rounded-5 transition-colors duration-200',
                'cursor:pointer;white-space:nowrap;font-weight:500;'
            ), before);
            break;
        }
        var mobile_toggle = document.querySelector('[data-testid="mobile-theme-toggle"]');
        if (mobile_toggle && mobile_toggle.parentNode) {
            mobile_toggle.parentNode.insertBefore(make_button(
                'text-sm-medium text-[var(--ink-gray-7)]',
                'cursor:pointer;white-space:nowrap;margin-right:4px;'
            ), mobile_toggle);
        }
    }

    function set_busy(text) {
        buttons.forEach(function (b) {
            b.textContent = text;
            b.classList.add('disabled');
            b.style.opacity = '0.5';
            b.style.pointerEvents = 'none';
        });
    }

    function set_idle() {
        buttons.forEach(function (b) {
            b.textContent = LABEL;
            b.classList.remove('disabled');
            b.style.opacity = '';
            b.style.pointerEvents = '';
        });
    }

    // ── Server ───────────────────────────────────────────────────────────

    // Returns {status, has_pdf, file_url, build_number, generated_at, ...};
    // the server queues an update when the PDF is older than the wiki content.
    function get_pdf_state(lang) {
        return fetch('/api/method/wiki_pdf.api.get_pdf?lang=' + encodeURIComponent(lang), {
            headers: { Accept: 'application/json' },
            credentials: 'same-origin'
        }).then(function (response) {
            return response.json().then(function (data) {
                if (!response.ok) {
                    var msg = 'Could not check the PDF. Please try again.';
                    try { msg = JSON.parse(JSON.parse(data._server_messages)[0]).message || msg; } catch (e) {}
                    throw new Error(msg);
                }
                return data.message;
            });
        });
    }

    function download(state) {
        var a = document.createElement('a');
        a.href = state.file_url;
        a.download = 'Creche_Guideline.pdf';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    }

    // ── Waiting for an update ────────────────────────────────────────────

    var ticker = null;      // updates the button's elapsed-time text every second
    var wait_state = null;  // latest state from the server while waiting

    function format_elapsed(ms) {
        var s = Math.max(0, Math.floor(ms / 1000));
        return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2);
    }

    // "45 / 79 pages" / "Waiting in queue (#2)"
    function progress_text(state) {
        if (state.status === 'Queued') {
            return state.queue_position > 1 ? 'Waiting in queue (#' + state.queue_position + ')' : 'Starting soon';
        }
        var p = state.progress;
        if (!p) return null;
        if (p.phase === 'rendering') return 'Creating the PDF';
        return p.done + ' / ' + p.total + ' pages';
    }

    // Button: "Updating PDF (ta) 45/79" / "Waiting in queue (#3)... 0:42"
    function render_wait(lang, started_at) {
        var s = wait_state || {};
        var p = s.status === 'Updating' ? s.progress : null;
        if (p && p.phase === 'rendering') {
            set_busy('Creating PDF (' + lang + ')...');
        } else if (p) {
            set_busy('Updating PDF (' + lang + ') ' + p.done + '/' + p.total);
        } else if (s.status === 'Queued') {
            var label = s.queue_position > 1
                ? 'Waiting in queue (#' + s.queue_position + ')'
                : 'Starting update (' + lang + ')';
            set_busy(label + '... ' + format_elapsed(Date.now() - started_at));
        } else {
            set_busy('Updating PDF (' + lang + ')... ' + format_elapsed(Date.now() - started_at));
        }
    }

    function stop_waiting() {
        if (ticker) clearInterval(ticker);
        ticker = null;
        wait_state = null;
        set_idle();
    }

    // Poll until the PDF matches the latest wiki content, then download it.
    function wait_for_latest(lang, started_at, state) {
        if (state) wait_state = state;
        render_wait(lang, started_at);
        if (!ticker) {
            ticker = setInterval(function () { render_wait(lang, started_at); }, 1000);
        }
        setTimeout(function () {
            get_pdf_state(lang).then(function (state) {
                if (state.status === 'Up to Date') {
                    stop_waiting();
                    download(state);
                    show_toast('The latest PDF has been downloaded (took '
                        + format_elapsed(Date.now() - started_at) + ').');
                } else if (state.status === 'Failed') {
                    stop_waiting();
                    show_status_popup(lang, state);
                } else if (Date.now() - started_at > MAX_WAIT_MS) {
                    stop_waiting();
                    show_message('PDF Still Updating',
                        'The PDF is taking longer than usual to update. Please try again later.');
                } else {
                    wait_for_latest(lang, started_at, state);
                }
            }).catch(function (err) {
                stop_waiting();
                show_message('Download Error', escape_html(err.message));
            });
        }, POLL_INTERVAL_MS);
    }

    // ── Popup ────────────────────────────────────────────────────────────

    // Uses the site's theme variables where they exist (Frappe's on Wiki 2,
    // frappe-ui's on Wiki 3) with fallbacks, and follows dark mode on both
    // (Wiki 2: body.dark, Wiki 3: html[data-theme="dark"]).
    function ensure_styles() {
        if (document.getElementById('wpdf-popup-styles')) return;
        var dark = 'body.dark, html[data-theme="dark"]';
        var css = ''
            + '.wpdf-overlay { position: fixed; inset: 0; z-index: 2000; background: rgba(15, 23, 42, 0.45);'
            + '  display: flex; align-items: flex-start; justify-content: center; padding: 8vh 16px 16px; overflow-y: auto; }'
            + '.wpdf-modal { width: 100%; max-width: 500px; background: #fff; color: #1f272e; border-radius: 14px;'
            + '  box-shadow: 0 24px 48px rgba(0, 0, 0, 0.22); font-family: inherit; }'
            + '.wpdf-modal-head { display: flex; align-items: center; justify-content: space-between; padding: 18px 20px 4px; }'
            + '.wpdf-modal-title { font-size: 18px; font-weight: 600; margin: 0; }'
            + '.wpdf-close { border: 0; background: transparent; font-size: 22px; line-height: 1; cursor: pointer; color: #7c7c7c; padding: 4px 6px; }'
            + '.wpdf-modal-body { padding: 12px 20px; }'
            + '.wpdf-modal-foot { display: flex; justify-content: flex-end; gap: 8px; padding: 6px 20px 18px; flex-wrap: wrap; }'
            + '.wpdf-btn { border: 0; border-radius: 8px; padding: 8px 14px; font-size: 14px; font-weight: 500; cursor: pointer; font-family: inherit; }'
            + '.wpdf-btn.primary { background: #171717; color: #fff; }'
            + '.wpdf-btn.secondary { background: #f3f3f3; color: #1f272e; }'
            + '.wpdf-hero { display: flex; gap: 14px; align-items: flex-start; padding: 16px; border-radius: 12px; }'
            + '.wpdf-hero.green { background: #e4f5e9; } .wpdf-hero.orange { background: #fff1e7; }'
            + '.wpdf-hero.red { background: #fff0f0; } .wpdf-hero.blue { background: #edf6fd; }'
            + '.wpdf-icon { flex: none; width: 40px; height: 40px; border-radius: 50%; display: flex;'
            + '  align-items: center; justify-content: center; color: #fff; }'
            + '.wpdf-hero.green .wpdf-icon { background: #30a66d; } .wpdf-hero.orange .wpdf-icon { background: #e86c13; }'
            + '.wpdf-hero.red .wpdf-icon { background: #e03636; } .wpdf-hero.blue .wpdf-icon { background: #2490ef; }'
            + '.wpdf-headline { font-size: 16px; font-weight: 600; line-height: 1.3; }'
            + '.wpdf-sub { margin-top: 4px; font-size: 13px; line-height: 1.5; opacity: 0.85; }'
            + '.wpdf-details { margin-top: 14px; border: 1px solid #ededed; border-radius: 10px; overflow: hidden; }'
            + '.wpdf-row { display: flex; justify-content: space-between; gap: 12px; padding: 9px 14px; font-size: 13px; }'
            + '.wpdf-row + .wpdf-row { border-top: 1px solid #ededed; }'
            + '.wpdf-row .k { color: #7c7c7c; } .wpdf-row .v { font-weight: 500; text-align: right; }'
            + '.wpdf-pill { display: inline-block; padding: 1px 9px; border-radius: 999px; font-size: 12px; font-weight: 600; }'
            + '.wpdf-pill.green { background: #e4f5e9; color: #16794c; } .wpdf-pill.orange { background: #fff1e7; color: #c2410c; }'
            + '.wpdf-pill.red { background: #fff0f0; color: #c53030; } .wpdf-pill.blue { background: #edf6fd; color: #1a6fba; }'
            + '.wpdf-note { display: flex; gap: 10px; margin-top: 14px; padding: 11px 14px; border-radius: 10px;'
            + '  font-size: 13px; line-height: 1.5; background: #f4f5f6; }'
            + '.wpdf-note svg { flex: none; margin-top: 2px; color: #7c7c7c; }'
            + '.wpdf-toast { position: fixed; right: 20px; bottom: 20px; z-index: 2100; max-width: 360px; padding: 12px 16px;'
            + '  border-radius: 10px; background: #171717; color: #fff; font-size: 14px; box-shadow: 0 12px 24px rgba(0,0,0,0.2); }'
            + dark.split(', ').map(function (d) { return d + ' .wpdf-modal'; }).join(', ') + ' { background: #1c1c1c; color: #ededed; }'
            + dark.split(', ').map(function (d) { return d + ' .wpdf-btn.secondary'; }).join(', ') + ' { background: #2e2e2e; color: #ededed; }'
            + dark.split(', ').map(function (d) { return d + ' .wpdf-btn.primary'; }).join(', ') + ' { background: #ededed; color: #171717; }'
            + dark.split(', ').map(function (d) { return d + ' .wpdf-details, ' + d + ' .wpdf-row + .wpdf-row'; }).join(', ') + ' { border-color: #333; }'
            + dark.split(', ').map(function (d) { return d + ' .wpdf-note'; }).join(', ') + ' { background: #262626; }'
            + dark.split(', ').map(function (d) { return d + ' .wpdf-hero'; }).join(', ') + ' { color: #1f272e; }';
        var el = document.createElement('style');
        el.id = 'wpdf-popup-styles';
        el.textContent = css;
        document.head.appendChild(el);
    }

    // A small modal: {title, body_html, primary: [label, fn], secondary: [label, fn]}
    function open_dialog(opts) {
        ensure_styles();
        var overlay = document.createElement('div');
        overlay.className = 'wpdf-overlay';
        overlay.innerHTML = '<div class="wpdf-modal" role="dialog" aria-modal="true">'
            + '<div class="wpdf-modal-head"><h3 class="wpdf-modal-title"></h3>'
            + '<button type="button" class="wpdf-close" aria-label="Close">&times;</button></div>'
            + '<div class="wpdf-modal-body"></div><div class="wpdf-modal-foot"></div></div>';
        overlay.querySelector('.wpdf-modal-title').textContent = opts.title;
        overlay.querySelector('.wpdf-modal-body').innerHTML = opts.body_html;

        function close() {
            document.removeEventListener('keydown', on_key);
            if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        }
        function on_key(e) { if (e.key === 'Escape') close(); }

        var foot = overlay.querySelector('.wpdf-modal-foot');
        [['secondary', opts.secondary], ['primary', opts.primary]].forEach(function (pair) {
            if (!pair[1]) return;
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'wpdf-btn ' + pair[0];
            b.textContent = pair[1][0];
            b.addEventListener('click', function () { close(); if (pair[1][1]) pair[1][1](); });
            foot.appendChild(b);
        });
        overlay.querySelector('.wpdf-close').addEventListener('click', close);
        overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
        document.addEventListener('keydown', on_key);
        document.body.appendChild(overlay);
        var primary = foot.querySelector('.wpdf-btn.primary');
        if (primary) primary.focus();
        return { hide: close };
    }

    function show_message(title, html) {
        open_dialog({ title: title, body_html: '<p style="margin:0;font-size:14px;line-height:1.5">' + html + '</p>', primary: ['OK'] });
    }

    function show_toast(text) {
        ensure_styles();
        var t = document.createElement('div');
        t.className = 'wpdf-toast';
        t.textContent = text;
        document.body.appendChild(t);
        setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 6000);
    }

    var ICONS = {
        check: '<path d="M20 6 9 17l-5-5"/>',
        clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
        alert: '<path d="M12 9v4"/><path d="M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
        info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
        hourglass: '<path d="M6 2h12"/><path d="M6 22h12"/><path d="M6 2c0 5 6 5 6 10S6 17 6 22"/><path d="M18 2c0 5-6 5-6 10s6 5 6 10"/>'
    };

    function icon(name, size) {
        size = size || 20;
        return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
            + ' stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">' + ICONS[name] + '</svg>';
    }

    // "23 Sep 2026, 02:21 PM · 15 minutes ago"
    function updated_text(state) {
        if (!state.generated_label) return null;
        var text = escape_html(state.generated_label);
        var ago = state.generated_at ? time_ago(state.generated_at) : '';
        return ago ? text + ' &middot; ' + ago : text;
    }

    function popup_html(opts, state) {
        var rows = [['Language', escape_html(state.language_name || state.language)]];
        if (state.build_number) rows.push(['Version', 'V' + state.build_number]);
        var updated = updated_text(state);
        if (updated) rows.push(['Last updated', updated]);
        rows.push(['Latest changes', '<span class="wpdf-pill ' + opts.tone + '">' + opts.pill + '</span>']);
        var progress = state.status !== 'Up to Date' && state.status !== 'Failed' ? progress_text(state) : null;
        if (progress) rows.push(['Update progress', progress]);

        return '<div class="wpdf-hero ' + opts.tone + '">'
            + '<div class="wpdf-icon">' + icon(opts.icon) + '</div>'
            + '<div><div class="wpdf-headline">' + opts.headline + '</div>'
            + '<div class="wpdf-sub">' + opts.sub + '</div></div>'
            + '</div>'
            + '<div class="wpdf-details">'
            + rows.map(function (r) {
                return '<div class="wpdf-row"><span class="k">' + r[0] + '</span><span class="v">' + r[1] + '</span></div>';
            }).join('')
            + '</div>'
            + (opts.note ? '<div class="wpdf-note">' + icon('info', 16) + '<div>' + opts.note + '</div></div>' : '');
    }

    // Shown on every click: tells the user whether the PDF has the latest
    // wiki changes before anything downloads.
    function show_status_popup(lang, state) {
        var dl = function () { download(state); };
        var wait = function () { wait_for_latest(lang, Date.now(), state); };
        var opts;

        if (state.status === 'Up to Date') {
            opts = {
                tone: 'green', icon: 'check', pill: 'Included',
                headline: 'Your PDF is up to date',
                sub: 'It includes all the latest wiki changes.',
                primary: ['Download PDF', dl]
            };
        } else if (!state.has_pdf && state.status === 'Failed') {
            opts = {
                tone: 'red', icon: 'alert', pill: 'Not available',
                headline: 'PDF not available yet',
                sub: 'The PDF for this language could not be generated. Please try again later.',
                primary: ['Close']
            };
        } else if (!state.has_pdf) {
            opts = {
                tone: 'blue', icon: 'hourglass', pill: 'Preparing',
                headline: 'Your PDF is being prepared',
                sub: 'This is the first PDF for this language. It takes a few minutes.',
                note: 'Keep this page open &mdash; the PDF will download automatically when it is ready.',
                primary: ['Wait and Download', wait]
            };
        } else if (state.status === 'Failed') {
            opts = {
                tone: 'red', icon: 'alert', pill: 'Not included',
                headline: 'PDF could not be updated',
                sub: 'The wiki content has changed, but the latest changes could not be added to the PDF.',
                note: '<b>Only the older version is available.</b> It does not include the most recent changes.',
                primary: ['Download Older Version', dl]
            };
        } else {
            opts = {
                tone: 'orange', icon: 'clock', pill: 'Being added',
                headline: 'A newer version is on its way',
                sub: 'The wiki content has changed. The latest changes are being added to the PDF now &mdash; this takes a few minutes.',
                note: '<b>Downloading now gives you the older version</b>, without the most recent changes. '
                    + 'Choose <b>Wait for Latest</b> and it will download automatically when ready.',
                primary: ['Wait for Latest', wait],
                secondary: ['Download Older Version', dl]
            };
        }

        open_dialog({
            title: 'Download PDF',
            body_html: popup_html(opts, state),
            primary: opts.primary,
            secondary: opts.secondary
        });
    }

    function on_click(e) {
        e.preventDefault();
        if (this.classList.contains('disabled')) return;

        var lang = get_selected_language();
        set_busy('Checking PDF... (' + lang + ')');

        get_pdf_state(lang).then(function (state) {
            set_idle();
            show_status_popup(lang, state);
        }).catch(function (err) {
            set_idle();
            show_message('Download Error', escape_html(err.message));
        });
    }

    // The header is rendered by the server, but Wiki 2 builds parts of the
    // navbar after load, so try again shortly if it isn't there yet.
    on_ready(function () {
        add_buttons();
        if (!buttons.length) setTimeout(add_buttons, 600);
    });
})();

// "Page X of N" next to "last updated" on each wiki page, numbered in sidebar
// order.
// - Wiki 2: added once to .wiki-page-meta.
// - Wiki 3: added inside #wiki-last-updated. Wiki 3 changes pages without a
//   reload and rewrites that element's text each time, so it is watched and
//   the number put back.
(function () {
    function current_route() {
        var store = window.Alpine && Alpine.store && Alpine.store('navigation');
        if (store && store.currentRoute) return String(store.currentRoute).replace(/^\/+|\/+$/g, '');
        return decodeURIComponent(window.location.pathname).replace(/^\/+|\/+$/g, '');
    }

    function index_of(routes, route) {
        for (var i = 0; i < routes.length; i++) {
            if (routes[i] === route) return i;
        }
        return -1;
    }

    function make_label(tag, index, total) {
        var label = document.createElement(tag);
        label.className = 'wiki-page-number';
        label.textContent = 'Page ' + (index + 1) + ' of ' + total;
        return label;
    }

    function wiki2() {
        var meta = document.querySelector('.wiki-page-meta');
        // The sidebar is rendered twice (desktop and mobile); use the first.
        var sidebar = document.querySelector('.web-sidebar');
        if (!meta || !sidebar || meta.querySelector('.wiki-page-number')) return;

        // Match on the URL: the server-rendered "sidebar-item-active" class
        // comes from a cached sidebar and can point at the wrong page.
        var routes = Array.prototype.map.call(
            sidebar.querySelectorAll('.sidebar-item[data-type="Wiki Page"]'),
            function (el) { return el.getAttribute('data-route'); }
        );
        var index = index_of(routes, current_route());
        if (index < 0) return;

        var label = make_label('span', index, routes.length);
        label.classList.add('text-muted');
        label.style.marginRight = '1rem';
        meta.insertBefore(label, meta.firstChild);
    }

    function wiki3_routes() {
        // The desktop and mobile sidebars both list every page; use the first.
        var first = document.querySelector('a.wiki-link[data-route]');
        var nav = first && first.closest('nav');
        if (!nav) return [];
        var routes = [];
        nav.querySelectorAll('a.wiki-link[data-route]').forEach(function (a) {
            var route = a.getAttribute('data-route');
            if (index_of(routes, route) < 0) routes.push(route);
        });
        return routes;
    }

    function wiki3() {
        var updated = document.getElementById('wiki-last-updated');
        if (!updated) return;

        function refresh() {
            var routes = wiki3_routes();
            var index = index_of(routes, current_route());
            var label = updated.querySelector('.wiki-page-number');
            if (index < 0) {
                if (label) label.remove();
                return;
            }
            var text = 'Page ' + (index + 1) + ' of ' + routes.length;
            if (label && label.textContent === text) return;
            if (label) label.remove();
            label = make_label('span', index, routes.length);
            label.style.marginRight = '0.75rem';
            label.style.paddingRight = '0.75rem';
            label.style.borderRight = '1px solid currentColor';
            updated.insertBefore(label, updated.firstChild);
        }

        refresh();
        new MutationObserver(refresh).observe(updated, { childList: true });
        // Back/forward buttons also change the page.
        window.addEventListener('popstate', function () { setTimeout(refresh, 300); });
    }

    function add_page_number() {
        if (document.getElementById('wiki-last-updated')) wiki3();
        else wiki2();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', add_page_number);
    } else {
        add_page_number();
    }
})();

// Image alignment on Wiki 3 pages. The Wiki 3 editor saves images as markdown
// and drops any styles, so the "Image settings" button in the editor
// (wiki_pdf_editor.bundle.js) stores the layout in the image title instead,
// e.g. ![photo](/files/a.png "align=wrap-left width=300px"). Here the title is
// turned into styles. pdf.py does the same for the PDF.
(function () {
    var ALIGN = /(?:^|\s)align=(left|center|right|wrap-left|wrap-right)(?=\s|$)/;
    var WIDTH = /(?:^|\s)width=(\d+(?:\.\d+)?(?:px|%))(?=\s|$)/;

    function layout_css(align, width) {
        var styles = [];
        if (align === 'wrap-left') styles.push('float:left', 'margin:0.3em 1.2em 0.8em 0');
        else if (align === 'wrap-right') styles.push('float:right', 'margin:0.3em 0 0.8em 1.2em');
        else if (align === 'left') styles.push('display:block', 'margin:1em auto 1em 0');
        else if (align === 'center') styles.push('display:block', 'margin:1em auto');
        else if (align === 'right') styles.push('display:block', 'margin:1em 0 1em auto');

        if (width) styles.push('width:' + width);
        else if (align === 'wrap-left' || align === 'wrap-right') styles.push('width:40%');
        styles.push('max-width:100%', 'height:auto');
        return styles.join(';');
    }

    function next_block(el) {
        var next = el.nextElementSibling;
        while (next && next.tagName === 'BR') next = next.nextElementSibling;
        return next;
    }

    function apply_layouts(root) {
        root.querySelectorAll('img[title]').forEach(function (img) {
            var title = img.getAttribute('title') || '';
            var align = (title.match(ALIGN) || [])[1] || '';
            var width = (title.match(WIDTH) || [])[1] || '';
            if (!align && !width) return;

            img.setAttribute('style', layout_css(align, width));
            var rest = title.replace(/(?:^|\s)(align|width)=\S+/g, '').trim();
            if (rest) img.setAttribute('title', rest);
            else img.removeAttribute('title');

            // Wrapping stops after the paragraph that follows the image.
            if (align === 'wrap-left' || align === 'wrap-right') {
                var block = img.parentElement && img.parentElement.tagName === 'P' ? img.parentElement : img;
                // Wiki 3 puts an image on its own line (any text beside it is
                // its caption), so the paragraph after it is the one that wraps.
                var wrapped = next_block(block);
                var after = wrapped && next_block(wrapped);
                if (after) after.style.clear = 'both';
            }
        });
    }

    function init() {
        var content = document.getElementById('wiki-content');
        if (!content) return;
        apply_layouts(content);
        // Wiki 3 swaps the page content in place when you move between pages.
        new MutationObserver(function () { apply_layouts(content); })
            .observe(content, { childList: true });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();

// "Image settings" button in the Wiki 2 page editor toolbar: put the cursor on
// an image line, click it, and pick the alignment and width. The image is
// rewritten as an <img> with inline styles, which the wiki page and the PDF
// both follow (inline styles override the PDF's centred-image default).
(function () {
    var WRAP_END = '<div style="clear:both" data-wrap-end="1"></div>';
    var MD_IMAGE = /!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)/;
    var HTML_IMAGE = /<img\b[^>]*>/i;

    var ALIGN_OPTIONS = [
        { value: 'default', label: 'Default (as uploaded)' },
        { value: 'left', label: 'Left' },
        { value: 'center', label: 'Center' },
        { value: 'right', label: 'Right' },
        { value: 'wrap-left', label: 'Image left, text wraps on the right' },
        { value: 'wrap-right', label: 'Image right, text wraps on the left' }
    ];

    var ICON = '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" '
        + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        + '<rect x="3" y="4" width="9" height="8" rx="1"/><path d="M15 5h6M15 9h6M3 16h18M3 20h18"/></svg>';

    function get_editor() {
        var el = document.getElementById('wiki-editor');
        return el && el.env && el.env.editor;
    }

    function escape_attr(text) {
        return String(text == null ? '' : text)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
    }

    // The first image on a line: {start, end, src, alt, align, width}
    function find_image(line) {
        var m = line.match(MD_IMAGE);
        if (m) {
            return { start: m.index, end: m.index + m[0].length, src: m[2], alt: m[1], align: 'default', width: '' };
        }
        m = line.match(HTML_IMAGE);
        if (!m) return null;

        var img = new DOMParser().parseFromString(m[0], 'text/html').querySelector('img');
        if (!img) return null;
        var style = img.style;
        var align = 'default';
        if (style.float === 'left') align = 'wrap-left';
        else if (style.float === 'right') align = 'wrap-right';
        else if (style.marginLeft === 'auto' && style.marginRight === 'auto') align = 'center';
        else if (style.marginRight === 'auto') align = 'left';
        else if (style.marginLeft === 'auto') align = 'right';

        return {
            start: m.index,
            end: m.index + m[0].length,
            src: img.getAttribute('src') || '',
            alt: img.getAttribute('alt') || '',
            align: align,
            width: style.width || img.getAttribute('width') || ''
        };
    }

    function normalise_width(width) {
        width = String(width || '').trim();
        if (/^\d+(\.\d+)?$/.test(width)) return width + 'px';
        if (/^\d+(\.\d+)?(px|%)$/.test(width)) return width;
        return '';
    }

    function image_markup(image, align, width) {
        width = normalise_width(width);
        if (align === 'default' && !width) {
            return '![' + image.alt + '](' + image.src + ')';
        }

        var styles = [];
        if (align === 'wrap-left') styles.push('float:left', 'margin:0.3em 1.2em 0.8em 0');
        else if (align === 'wrap-right') styles.push('float:right', 'margin:0.3em 0 0.8em 1.2em');
        else if (align === 'left') styles.push('display:block', 'margin:1em auto 1em 0');
        else if (align === 'center') styles.push('display:block', 'margin:1em auto');
        else if (align === 'right') styles.push('display:block', 'margin:1em 0 1em auto');

        if (width) styles.push('width:' + width);
        else if (align === 'wrap-left' || align === 'wrap-right') styles.push('width:40%');
        styles.push('max-width:100%', 'height:auto');

        return '<img src="' + escape_attr(image.src) + '" alt="' + escape_attr(image.alt)
            + '" style="' + styles.join(';') + '">';
    }

    // The wrap-end marker that belongs to the image on `row`: the first one
    // after it, before the next image.
    function find_wrap_end(doc, row) {
        for (var r = row + 1; r < doc.getLength(); r++) {
            var line = doc.getLine(r);
            if (line.indexOf('data-wrap-end') !== -1) return r;
            if (MD_IMAGE.test(line) || HTML_IMAGE.test(line)) return -1;
        }
        return -1;
    }

    // Last line of the paragraph that follows the image, so text wrapping
    // stops there instead of running into the next heading.
    function paragraph_end(doc, row) {
        var last = doc.getLength() - 1;
        // Text on the image's own line is the paragraph that wraps.
        var has_text = doc.getLine(row).replace(MD_IMAGE, '').replace(HTML_IMAGE, '').trim();
        var r = has_text ? row : row + 1;
        while (r <= last && !doc.getLine(r).trim()) r++;
        if (r > last) return row;
        while (r < last && doc.getLine(r + 1).trim()) r++;
        return r;
    }

    function apply(editor, row, image, align, width) {
        var doc = editor.session.getDocument();

        var old_end = find_wrap_end(doc, row);
        if (old_end !== -1) {
            // Also drop the blank line that was added before the marker.
            var first = old_end - 1 > row && !doc.getLine(old_end - 1).trim() ? old_end - 1 : old_end;
            doc.removeFullLines(first, old_end);
        }

        doc.replace(
            { start: { row: row, column: image.start }, end: { row: row, column: image.end } },
            image_markup(image, align, width)
        );

        if (align === 'wrap-left' || align === 'wrap-right') {
            var end = paragraph_end(doc, row);
            var after = end + 1 < doc.getLength() ? doc.getLine(end + 1) : '';
            var lines = ['', WRAP_END];
            if (after.trim()) lines.push('');
            doc.insertFullLines(end + 1, lines);
        }
        editor.focus();
    }

    function open_settings() {
        var editor = get_editor();
        if (!editor || !window.frappe || !frappe.ui || !frappe.ui.Dialog) return;

        var row = editor.getCursorPosition().row;
        var image = find_image(editor.session.getLine(row));
        if (!image) {
            frappe.msgprint({
                title: 'Image settings',
                message: 'Click on the line with the image first, then press this button again.'
            });
            return;
        }

        // Website pages load frappe.ui.Dialog but not the form controls it
        // needs for its fields, so load those first.
        if (!(frappe.ui.form && frappe.ui.form.make_control)) {
            frappe.require('controls.bundle.js', function () {
                show_dialog(editor, row, image);
            });
            return;
        }
        show_dialog(editor, row, image);
    }

    function show_dialog(editor, row, image) {
        var dialog = new frappe.ui.Dialog({
            title: 'Image settings',
            fields: [
                {
                    fieldname: 'align', fieldtype: 'Select', label: 'Alignment',
                    options: ALIGN_OPTIONS, default: image.align
                },
                {
                    fieldname: 'width', fieldtype: 'Data', label: 'Width',
                    default: image.width,
                    description: 'e.g. 300 (pixels) or 50%. Leave empty for the original size.'
                }
            ],
            primary_action_label: 'Apply',
            primary_action: function (values) {
                if (values.width && !normalise_width(values.width)) {
                    frappe.msgprint('Width must be a number (pixels) or a percentage, like 300 or 50%.');
                    return;
                }
                dialog.hide();
                apply(editor, row, image, values.align || 'default', values.width);
            }
        });
        dialog.show();
    }

    function add_button() {
        var image_btn = document.querySelector('[data-mde-button="image"]');
        if (!image_btn || document.querySelector('[data-mde-button="image-settings"]')) return;

        var btn = document.createElement('button');
        btn.type = 'button';
        btn.setAttribute('data-mde-button', 'image-settings');
        btn.title = 'Image settings (alignment and size)';
        btn.innerHTML = ICON;
        btn.addEventListener('click', function (e) {
            e.preventDefault();
            open_settings();
        });
        image_btn.parentNode.insertBefore(btn, image_btn.nextSibling);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', add_button);
    } else {
        add_button();
    }
})();
