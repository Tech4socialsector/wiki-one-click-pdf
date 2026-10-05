(() => {
  // ../wiki_pdf/wiki_pdf/public/js/wiki_pdf.bundle.js
  frappe.ready(function() {
    if ($(".wiki-content").length > 0 || window.location.pathname.indexOf("/home") !== -1) {
      setTimeout(setup_wiki_pdf_download, 600);
    }
  });
  function get_selected_language() {
    var combo = document.querySelector(".goog-te-combo");
    if (combo && combo.value && combo.value !== "" && combo.value !== "en") {
      return combo.value.split("-")[0];
    }
    var cookies = document.cookie.split(";");
    for (var i = 0; i < cookies.length; i++) {
      var c = cookies[i].trim();
      if (c.indexOf("googtrans=") === 0) {
        var val = c.substring("googtrans=".length).trim();
        var parts = val.split("/");
        if (parts.length >= 3 && parts[2] && parts[2] !== "en" && parts[2] !== "auto") {
          return parts[2];
        }
      }
    }
    return "en";
  }
  function setup_wiki_pdf_download() {
    if ($("#btn-wiki-pdf-dl").length > 0)
      return;
    var $navbar = $(".navbar-nav").first();
    if ($navbar.length === 0)
      return;
    var $btn = $("<a>").attr("id", "btn-wiki-pdf-dl").attr("href", "#").addClass("navbar-link mr-4 d-print-none").css({ "font-weight": "600", "cursor": "pointer", "color": "var(--text-color)", "opacity": "0.9" }).text("Download PDF");
    $btn.hover(
      function() {
        $(this).css("opacity", "1").css("text-decoration", "underline");
      },
      function() {
        $(this).css("opacity", "0.9").css("text-decoration", "none");
      }
    );
    var original_text = $btn.text();
    var POLL_INTERVAL_MS = 1e4;
    var MAX_WAIT_MS = 90 * 60 * 1e3;
    function set_busy(text) {
      $btn.text(text).addClass("disabled").css("opacity", "0.5").css("pointer-events", "none");
    }
    function set_idle() {
      $btn.text(original_text).removeClass("disabled").css("opacity", "0.9").css("pointer-events", "auto");
    }
    function get_pdf_state(lang) {
      return fetch("/api/method/wiki_pdf.api.get_pdf?lang=" + encodeURIComponent(lang)).then(function(response) {
        return response.json().then(function(data) {
          if (!response.ok) {
            var msg = "Could not check the PDF. Please try again.";
            try {
              msg = JSON.parse(JSON.parse(data._server_messages)[0]).message || msg;
            } catch (e) {
            }
            throw new Error(msg);
          }
          return data.message;
        });
      });
    }
    function download(state) {
      var a = document.createElement("a");
      a.href = state.file_url;
      a.download = "Creche_Guideline.pdf";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    }
    function show_error(message) {
      frappe.msgprint({ title: "Download Error", message, indicator: "red" });
    }
    var ticker = null;
    var wait_state = null;
    function format_elapsed(ms) {
      var s = Math.max(0, Math.floor(ms / 1e3));
      return Math.floor(s / 60) + ":" + ("0" + s % 60).slice(-2);
    }
    function render_wait(lang, started_at) {
      var label = "Updating PDF (" + lang + ")";
      if (wait_state && wait_state.status === "Queued") {
        label = wait_state.queue_position > 1 ? "Waiting in queue (#" + wait_state.queue_position + ")" : "Starting update (" + lang + ")";
      }
      set_busy(label + "... " + format_elapsed(Date.now() - started_at));
    }
    function stop_waiting() {
      if (ticker)
        clearInterval(ticker);
      ticker = null;
      wait_state = null;
      set_idle();
    }
    function wait_for_latest(lang, started_at, state) {
      if (state)
        wait_state = state;
      render_wait(lang, started_at);
      if (!ticker) {
        ticker = setInterval(function() {
          render_wait(lang, started_at);
        }, 1e3);
      }
      setTimeout(function() {
        get_pdf_state(lang).then(function(state2) {
          if (state2.status === "Up to Date") {
            stop_waiting();
            download(state2);
            frappe.show_alert({
              message: "The latest PDF has been downloaded (took " + format_elapsed(Date.now() - started_at) + ").",
              indicator: "green"
            }, 6);
          } else if (state2.status === "Failed") {
            stop_waiting();
            show_status_popup(lang, state2);
          } else if (Date.now() - started_at > MAX_WAIT_MS) {
            stop_waiting();
            frappe.msgprint({
              title: "PDF Still Updating",
              message: "The PDF is taking longer than usual to update. Please try again later.",
              indicator: "orange"
            });
          } else {
            wait_for_latest(lang, started_at, state2);
          }
        }).catch(function(err) {
          stop_waiting();
          show_error(err.message);
        });
      }, POLL_INTERVAL_MS);
    }
    function ensure_popup_styles() {
      if (document.getElementById("wpdf-popup-styles"))
        return;
      var css = ".wpdf-popup { padding: 4px 0 2px; }.wpdf-hero { display: flex; gap: 14px; align-items: flex-start; padding: 16px; border-radius: 12px; }.wpdf-hero.green { background: var(--bg-green, #e4f5e9); }.wpdf-hero.orange { background: var(--bg-orange, #fff1e7); }.wpdf-hero.red { background: var(--bg-red, #fff0f0); }.wpdf-hero.blue { background: var(--bg-blue, #edf6fd); }.wpdf-icon { flex: none; width: 40px; height: 40px; border-radius: 50%; display: flex;  align-items: center; justify-content: center; color: #fff; }.wpdf-hero.green .wpdf-icon { background: var(--green-500, #30a66d); }.wpdf-hero.orange .wpdf-icon { background: var(--orange-500, #e86c13); }.wpdf-hero.red .wpdf-icon { background: var(--red-500, #e03636); }.wpdf-hero.blue .wpdf-icon { background: var(--blue-500, #2490ef); }.wpdf-headline { font-size: 16px; font-weight: 600; color: var(--heading-color, #1f272e); line-height: 1.3; }.wpdf-sub { margin-top: 4px; font-size: 13px; color: var(--text-color, #383838); line-height: 1.5; }.wpdf-details { margin-top: 14px; border: 1px solid var(--border-color, #ededed); border-radius: 10px; overflow: hidden; }.wpdf-row { display: flex; justify-content: space-between; gap: 12px; padding: 9px 14px; font-size: 13px; }.wpdf-row + .wpdf-row { border-top: 1px solid var(--border-color, #ededed); }.wpdf-row .k { color: var(--text-muted, #7c7c7c); }.wpdf-row .v { color: var(--text-color, #383838); font-weight: 500; text-align: right; }.wpdf-pill { display: inline-block; padding: 1px 9px; border-radius: 999px; font-size: 12px; font-weight: 600; }.wpdf-pill.green { background: var(--bg-green, #e4f5e9); color: var(--text-on-green, #16794c); }.wpdf-pill.orange { background: var(--bg-orange, #fff1e7); color: var(--text-on-orange, #c2410c); }.wpdf-pill.red { background: var(--bg-red, #fff0f0); color: var(--text-on-red, #c53030); }.wpdf-pill.blue { background: var(--bg-blue, #edf6fd); color: var(--text-on-blue, #1a6fba); }.wpdf-note { display: flex; gap: 10px; margin-top: 14px; padding: 11px 14px; border-radius: 10px;  font-size: 13px; line-height: 1.5; background: var(--subtle-fg, #f4f5f6); color: var(--text-color, #383838); }.wpdf-note svg { flex: none; margin-top: 2px; color: var(--text-muted, #7c7c7c); }";
      var el = document.createElement("style");
      el.id = "wpdf-popup-styles";
      el.textContent = css;
      document.head.appendChild(el);
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
      return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">' + ICONS[name] + "</svg>";
    }
    function updated_text(state) {
      if (!state.generated_label)
        return null;
      var text = frappe.utils.escape_html(state.generated_label);
      try {
        if (frappe.datetime && frappe.datetime.prettyDate && state.generated_at) {
          text += " &middot; " + frappe.datetime.prettyDate(state.generated_at);
        }
      } catch (e) {
      }
      return text;
    }
    function popup_html(opts, state) {
      var rows = [["Language", frappe.utils.escape_html(state.language_name || state.language)]];
      if (state.build_number)
        rows.push(["Version", "V" + state.build_number]);
      var updated = updated_text(state);
      if (updated)
        rows.push(["Last updated", updated]);
      rows.push(["Latest changes", '<span class="wpdf-pill ' + opts.tone + '">' + opts.pill + "</span>"]);
      return '<div class="wpdf-popup"><div class="wpdf-hero ' + opts.tone + '"><div class="wpdf-icon">' + icon(opts.icon) + '</div><div><div class="wpdf-headline">' + opts.headline + '</div><div class="wpdf-sub">' + opts.sub + '</div></div></div><div class="wpdf-details">' + rows.map(function(r) {
        return '<div class="wpdf-row"><span class="k">' + r[0] + '</span><span class="v">' + r[1] + "</span></div>";
      }).join("") + "</div>" + (opts.note ? '<div class="wpdf-note">' + icon("info", 16) + "<div>" + opts.note + "</div></div>" : "") + "</div>";
    }
    function show_status_popup(lang, state) {
      ensure_popup_styles();
      var d;
      var opts;
      var dl = function() {
        d.hide();
        download(state);
      };
      var wait = function() {
        d.hide();
        wait_for_latest(lang, Date.now(), state);
      };
      if (state.status === "Up to Date") {
        opts = {
          tone: "green",
          icon: "check",
          pill: "Included",
          headline: "Your PDF is up to date",
          sub: "It includes all the latest wiki changes.",
          primary: ["Download PDF", dl]
        };
      } else if (!state.has_pdf && state.status === "Failed") {
        opts = {
          tone: "red",
          icon: "alert",
          pill: "Not available",
          headline: "PDF not available yet",
          sub: "The PDF for this language could not be generated. Please try again later.",
          primary: ["Close", function() {
            d.hide();
          }]
        };
      } else if (!state.has_pdf) {
        opts = {
          tone: "blue",
          icon: "hourglass",
          pill: "Preparing",
          headline: "Your PDF is being prepared",
          sub: "This is the first PDF for this language. It takes a few minutes.",
          note: "Keep this page open &mdash; the PDF will download automatically when it is ready.",
          primary: ["Wait and Download", wait]
        };
      } else if (state.status === "Failed") {
        opts = {
          tone: "red",
          icon: "alert",
          pill: "Not included",
          headline: "PDF could not be updated",
          sub: "The wiki content has changed, but the latest changes could not be added to the PDF.",
          note: "<b>Only the older version is available.</b> It does not include the most recent changes.",
          primary: ["Download Older Version", dl]
        };
      } else {
        opts = {
          tone: "orange",
          icon: "clock",
          pill: "Being added",
          headline: "A newer version is on its way",
          sub: "The wiki content has changed. The latest changes are being added to the PDF now &mdash; this takes a few minutes.",
          note: "<b>Downloading now gives you the older version</b>, without the most recent changes. Choose <b>Wait for Latest</b> and it will download automatically when ready.",
          primary: ["Wait for Latest", wait],
          secondary: ["Download Older Version", dl]
        };
      }
      d = new frappe.ui.Dialog({
        title: "Download PDF",
        primary_action_label: opts.primary[0],
        primary_action: opts.primary[1],
        secondary_action_label: opts.secondary ? opts.secondary[0] : void 0,
        secondary_action: opts.secondary ? opts.secondary[1] : void 0
      });
      d.$body.append(popup_html(opts, state));
      d.show();
    }
    $btn.on("click", function(e) {
      e.preventDefault();
      if ($btn.hasClass("disabled"))
        return;
      var lang = get_selected_language();
      set_busy("Checking PDF... (" + lang + ")");
      get_pdf_state(lang).then(function(state) {
        set_idle();
        show_status_popup(lang, state);
      }).catch(function(err) {
        set_idle();
        show_error(err.message);
      });
    });
    var $target = $navbar.find(".sun-moon-container, .navbar-search").first();
    if ($target.length > 0) {
      $target.before($btn);
    } else {
      $navbar.prepend($btn);
    }
  }
})();
//# sourceMappingURL=wiki_pdf.bundle.TBLORIDF.js.map
