// "Image settings" for the Wiki 3 page editor (the /wiki/... Vue app).
//
// Click an image in the editor and an "Image settings" button appears on it.
// The chosen alignment and width are stored in the image title, e.g.
//   ![photo](/files/a.png "align=wrap-left width=300px")
// because the Wiki 3 editor saves images as markdown and drops any styles.
// wiki_pdf.bundle.js turns the title into styles on the wiki page, and pdf.py
// does the same in the PDF.
//
// Added to the editor page by wiki_pdf.web.add_assets_to_wiki_pages. Plain
// JavaScript: the editor app has no jQuery or frappe.ui.
(function () {
    var ALIGN = /(?:^|\s)align=(left|center|right|wrap-left|wrap-right)(?=\s|$)/;
    var WIDTH = /(?:^|\s)width=(\d+(?:\.\d+)?(?:px|%))(?=\s|$)/;

    var ALIGN_OPTIONS = [
        ['', 'Default'],
        ['left', 'Left'],
        ['center', 'Center'],
        ['right', 'Right'],
        ['wrap-left', 'Image left, text wraps on the right'],
        ['wrap-right', 'Image right, text wraps on the left']
    ];

    var hooked = typeof WeakSet !== 'undefined' ? new WeakSet() : null;
    var active = null; // {editor, pos}
    var button = null;

    function parse_title(title) {
        title = title || '';
        return {
            align: (title.match(ALIGN) || [])[1] || '',
            width: (title.match(WIDTH) || [])[1] || '',
            rest: title.replace(/(?:^|\s)(align|width)=\S+/g, '').trim()
        };
    }

    function build_title(rest, align, width) {
        var parts = [];
        if (rest) parts.push(rest);
        if (align) parts.push('align=' + align);
        if (width) parts.push('width=' + width);
        return parts.join(' ') || null;
    }

    function normalise_width(width) {
        width = String(width || '').trim();
        if (/^\d+(\.\d+)?$/.test(width)) return width + 'px';
        if (/^\d+(\.\d+)?(px|%)$/.test(width)) return width;
        return '';
    }

    function image_dom(editor, pos) {
        var dom = editor.view.nodeDOM(pos);
        if (!dom || dom.nodeType !== 1) return null;
        return dom.tagName === 'IMG' ? dom : dom.querySelector('img');
    }

    // Show the layout in the editor too (without floats, which would make
    // the editor hard to use): width, and which side the image sits on.
    function preview(editor) {
        editor.state.doc.descendants(function (node, pos) {
            if (node.type.name !== 'image') return;
            var img = image_dom(editor, pos);
            if (!img) return;
            var layout = parse_title(node.attrs.title);
            var side = layout.align.replace('wrap-', '');
            img.style.width = layout.width || (layout.align.indexOf('wrap-') === 0 ? '40%' : '');
            img.style.marginLeft = side === 'center' || side === 'right' ? 'auto' : '';
            img.style.marginRight = side === 'center' || side === 'left' ? 'auto' : '';
            if (side) img.style.display = 'block';
        });
    }

    // ── Floating button on the selected image ──────────────────────────────
    function get_button() {
        if (button) return button;
        button = document.createElement('button');
        button.type = 'button';
        button.textContent = 'Image settings';
        button.setAttribute('style', [
            'position:fixed', 'z-index:50', 'display:none',
            'padding:4px 10px', 'font-size:13px', 'line-height:20px',
            'border-radius:6px', 'border:1px solid var(--outline-gray-2, #e2e2e2)',
            'background:var(--surface-white, #fff)', 'color:var(--ink-gray-8, #333)',
            'box-shadow:0 2px 6px rgba(0,0,0,0.15)', 'cursor:pointer'
        ].join(';'));
        // Keep the editor selection when the button is pressed.
        button.addEventListener('mousedown', function (e) { e.preventDefault(); });
        button.addEventListener('click', function (e) {
            e.preventDefault();
            if (active) open_dialog(active.editor, active.pos);
        });
        document.body.appendChild(button);
        return button;
    }

    function selected_image(editor) {
        var sel = editor.state.selection;
        if (!editor.isEditable || !sel.node || sel.node.type.name !== 'image') return null;
        return sel.from;
    }

    function place_button(editor) {
        var btn = get_button();
        var pos = selected_image(editor);
        var img = pos == null ? null : image_dom(editor, pos);
        if (!img) {
            if (active && active.editor === editor) {
                active = null;
                btn.style.display = 'none';
            }
            return;
        }
        active = { editor: editor, pos: pos };
        var rect = img.getBoundingClientRect();
        btn.style.display = 'block';
        btn.style.top = Math.max(8, rect.top + 8) + 'px';
        btn.style.left = Math.max(8, rect.right - btn.offsetWidth - 8) + 'px';
    }

    // ── Dialog ─────────────────────────────────────────────────────────────
    function open_dialog(editor, pos) {
        var node = editor.state.doc.nodeAt(pos);
        if (!node || node.type.name !== 'image') return;
        var layout = parse_title(node.attrs.title);

        var overlay = document.createElement('div');
        overlay.setAttribute('style', 'position:fixed;inset:0;z-index:100;background:rgba(0,0,0,0.35);'
            + 'display:flex;align-items:center;justify-content:center;padding:16px');

        var options = ALIGN_OPTIONS.map(function (o) {
            return '<label style="display:flex;align-items:center;gap:8px;padding:4px 0;cursor:pointer">'
                + '<input type="radio" name="wpdf-align" value="' + o[0] + '"'
                + (o[0] === layout.align ? ' checked' : '') + '> ' + o[1] + '</label>';
        }).join('');

        overlay.innerHTML = '<div role="dialog" aria-modal="true" style="width:100%;max-width:380px;'
            + 'background:var(--surface-white, #fff);color:var(--ink-gray-9, #111);border-radius:12px;'
            + 'padding:20px;box-shadow:0 10px 30px rgba(0,0,0,0.25);font-size:14px">'
            + '<div style="font-size:16px;font-weight:600;margin-bottom:12px">Image settings</div>'
            + '<div style="font-weight:500;margin-bottom:4px">Alignment</div>' + options
            + '<div style="font-weight:500;margin:12px 0 4px">Width</div>'
            + '<input type="text" class="wpdf-width" placeholder="e.g. 300 or 50%" style="width:100%;'
            + 'padding:6px 8px;border:1px solid var(--outline-gray-2, #ddd);border-radius:6px;'
            + 'background:var(--surface-gray-1, #fff);color:inherit">'
            + '<div style="font-size:12px;color:var(--ink-gray-5, #777);margin-top:4px">'
            + 'Pixels (300) or a percentage (50%). Leave empty for the original size.</div>'
            + '<div class="wpdf-error" style="font-size:12px;color:#d33;margin-top:4px;display:none">'
            + 'Width must be a number like 300 or a percentage like 50%.</div>'
            + '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">'
            + '<button type="button" class="wpdf-cancel" style="padding:6px 12px;border-radius:6px;'
            + 'border:1px solid var(--outline-gray-2, #ddd);background:transparent;color:inherit;cursor:pointer">Cancel</button>'
            + '<button type="button" class="wpdf-apply" style="padding:6px 12px;border-radius:6px;'
            + 'border:none;background:var(--ink-gray-9, #111);color:var(--surface-white, #fff);cursor:pointer">Apply</button>'
            + '</div></div>';

        var width_input = overlay.querySelector('.wpdf-width');
        width_input.value = layout.width;

        function close() {
            overlay.remove();
            document.removeEventListener('keydown', on_key, true);
            editor.commands.focus();
        }

        function apply() {
            var width = width_input.value.trim();
            if (width && !normalise_width(width)) {
                overlay.querySelector('.wpdf-error').style.display = 'block';
                return;
            }
            var checked = overlay.querySelector('input[name="wpdf-align"]:checked');
            var title = build_title(layout.rest, checked ? checked.value : '', normalise_width(width));

            // The document may have changed while the dialog was open.
            var current = editor.state.doc.nodeAt(pos);
            if (current && current.type.name === 'image') {
                editor.chain().command(function (props) {
                    props.tr.setNodeMarkup(pos, undefined, Object.assign({}, current.attrs, { title: title }));
                    return true;
                }).run();
            }
            close();
        }

        function on_key(e) {
            if (e.key === 'Escape') { e.stopPropagation(); close(); }
            if (e.key === 'Enter') { e.preventDefault(); apply(); }
        }

        overlay.addEventListener('mousedown', function (e) { if (e.target === overlay) close(); });
        overlay.querySelector('.wpdf-cancel').addEventListener('click', close);
        overlay.querySelector('.wpdf-apply').addEventListener('click', apply);
        document.addEventListener('keydown', on_key, true);
        document.body.appendChild(overlay);
        width_input.focus();
    }

    // ── Hook every editor the app creates ──────────────────────────────────
    function hook(el) {
        var editor = el.editor;
        if (!editor || !editor.on || (hooked && hooked.has(editor))) return;
        if (hooked) hooked.add(editor);

        editor.on('selectionUpdate', function () { place_button(editor); });
        editor.on('transaction', function () {
            preview(editor);
            place_button(editor);
        });
        editor.on('focus', function () { place_button(editor); });
        editor.on('blur', function () {
            setTimeout(function () { if (!editor.isFocused) hide_button(editor); }, 150);
        });
        editor.on('destroy', function () { hide_button(editor); });
        preview(editor);
    }

    function hide_button(editor) {
        if (active && active.editor === editor) {
            active = null;
            get_button().style.display = 'none';
        }
    }

    function scan() {
        document.querySelectorAll('.ProseMirror').forEach(hook);
    }

    function reposition() {
        if (active) place_button(active.editor);
    }

    function init() {
        scan();
        new MutationObserver(scan).observe(document.body, { childList: true, subtree: true });
        window.addEventListener('scroll', reposition, true);
        window.addEventListener('resize', reposition);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
