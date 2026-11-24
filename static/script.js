/* ==========================================================================
   MediFlow interactions
   ========================================================================== */
(function () {
  "use strict";

  var $ = function (selector, root) {
    return (root || document).querySelector(selector);
  };
  var $$ = function (selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  };
  var isTyping = function (target) {
    if (!target) return false;
    var tag = (target.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select" || target.isContentEditable;
  };

  /* Mobile sidebar ------------------------------------------------------- */
  var sidebar = $("[data-sidebar]");
  var scrim = $("[data-sidebar-scrim]");
  var menuToggle = $("[data-menu-toggle]");

  function setMenu(open) {
    if (!sidebar) return;
    sidebar.classList.toggle("is-open", open);
    if (scrim) scrim.classList.toggle("is-open", open);
    if (menuToggle) menuToggle.setAttribute("aria-expanded", String(open));
    document.body.classList.toggle("menu-open", open);
  }

  if (menuToggle) {
    menuToggle.addEventListener("click", function () {
      setMenu(!sidebar.classList.contains("is-open"));
    });
  }
  if (scrim) {
    scrim.addEventListener("click", function () {
      setMenu(false);
    });
  }
  $$(".nav-link").forEach(function (link) {
    link.addEventListener("click", function () {
      setMenu(false);
    });
  });

  /* Dropdown menus ------------------------------------------------------- */
  var menus = $$(".menu[data-menu]");

  function closeMenus(except) {
    menus.forEach(function (menu) {
      if (menu === except) return;
      menu.classList.remove("is-open");
      var trigger = $("[data-menu-trigger]", menu);
      if (trigger) trigger.setAttribute("aria-expanded", "false");
    });
  }

  menus.forEach(function (menu) {
    var trigger = $("[data-menu-trigger]", menu);
    if (!trigger) return;
    trigger.addEventListener("click", function (event) {
      event.stopPropagation();
      var open = !menu.classList.contains("is-open");
      closeMenus(menu);
      menu.classList.toggle("is-open", open);
      trigger.setAttribute("aria-expanded", String(open));
    });
  });

  document.addEventListener("click", function (event) {
    if (!event.target.closest || !event.target.closest("[data-menu]")) {
      closeMenus();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeMenus();
  });

  /* Global search -------------------------------------------------------- */
  var globalSearch = $("[data-global-search]");
  var globalSearchInput = $("[data-search-input]");
  var searchToggle = $("[data-search-toggle]");

  function focusSearch() {
    if (!globalSearchInput) return;
    document.body.classList.add("search-open");
    if (searchToggle) searchToggle.setAttribute("aria-expanded", "true");
    globalSearchInput.focus();
    globalSearchInput.select();
  }

  if (searchToggle && globalSearch) {
    searchToggle.addEventListener("click", function () {
      var open = !document.body.classList.contains("search-open");
      document.body.classList.toggle("search-open", open);
      searchToggle.setAttribute("aria-expanded", String(open));
      if (open) {
        window.setTimeout(function () {
          if (globalSearchInput) globalSearchInput.focus();
        }, 30);
      }
    });
  }

  document.addEventListener("keydown", function (event) {
    if (event.key === "/" && !isTyping(event.target) && !event.metaKey && !event.ctrlKey && !event.altKey) {
      event.preventDefault();
      focusSearch();
    }
  });

  /* Dialogs -------------------------------------------------------------- */
  function openDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.showModal === "function") {
      if (!dialog.open) dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
  }

  function closeDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.close === "function") {
      dialog.close();
    } else {
      dialog.removeAttribute("open");
    }
  }

  $$("[data-dialog-open]").forEach(function (button) {
    button.addEventListener("click", function () {
      openDialog($(button.getAttribute("data-dialog-open")));
    });
  });

  $$("[data-dialog-close]").forEach(function (button) {
    button.addEventListener("click", function () {
      closeDialog(button.closest("dialog"));
    });
  });

  $$("dialog[data-auto-open='true']").forEach(function (dialog) {
    openDialog(dialog);
  });

  $$("dialog").forEach(function (dialog) {
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) closeDialog(dialog);
    });
  });

  /* Confirmation dialog -------------------------------------------------- */
  var confirmDialog = $("#confirm-dialog");
  var confirmMessage = $("#confirm-message");
  var confirmTitle = $("#confirm-title");
  var pendingForm = null;

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || form.tagName !== "FORM" || !form.matches("form[data-confirm]")) {
      return;
    }
    if (form.dataset.confirmed === "true") {
      delete form.dataset.confirmed;
      return;
    }

    var message = form.getAttribute("data-confirm");
    if (!message) return;
    event.preventDefault();

    if (!confirmDialog || typeof confirmDialog.showModal !== "function") {
      if (window.confirm(message)) {
        form.dataset.confirmed = "true";
        form.requestSubmit();
      }
      return;
    }

    pendingForm = form;
    if (confirmMessage) confirmMessage.textContent = message;
    var title = form.getAttribute("data-confirm-title");
    if (confirmTitle) confirmTitle.textContent = title || "Please confirm";
    openDialog(confirmDialog);
  });

  $$("[data-confirm-cancel]").forEach(function (button) {
    button.addEventListener("click", function () {
      pendingForm = null;
      closeDialog(confirmDialog);
    });
  });

  var confirmAccept = $("[data-confirm-accept]");
  if (confirmAccept) {
    confirmAccept.addEventListener("click", function () {
      if (!pendingForm) return;
      var form = pendingForm;
      pendingForm = null;
      closeDialog(confirmDialog);
      form.dataset.confirmed = "true";
      form.requestSubmit();
    });
  }

  if (confirmDialog) {
    confirmDialog.addEventListener("close", function () {
      if (pendingForm) pendingForm = null;
    });
  }

  /* Password visibility -------------------------------------------------- */
  $$("[data-password-toggle]").forEach(function (button) {
    button.addEventListener("click", function () {
      var input = $(button.getAttribute("data-password-toggle"));
      if (!input) return;
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      button.setAttribute("aria-pressed", String(show));
      button.setAttribute("aria-label", show ? "Hide password" : "Show password");
      button.classList.toggle("is-showing", show);
    });
  });

  /* Print ---------------------------------------------------------------- */
  $$("[data-print]").forEach(function (button) {
    button.addEventListener("click", function () {
      window.print();
    });
  });

  /* Client-side table filters ------------------------------------------- */
  function controlsFor(attribute, selector) {
    return $$("[" + attribute + "]").filter(function (element) {
      return element.getAttribute(attribute) === selector;
    });
  }

  function applyTableFilter(tableSelector) {
    var table = $(tableSelector);
    if (!table) return;
    var filters = controlsFor("data-table-filter", tableSelector);
    var statuses = controlsFor("data-table-status", tableSelector);
    var counters = controlsFor("data-table-count", tableSelector);
    var term = filters.length ? (filters[0].value || "").toLowerCase().trim() : "";
    var status = statuses.length ? statuses[0].value : "";
    var rows = $$("tbody tr[data-search]", table);
    var visible = 0;

    rows.forEach(function (row) {
      var haystack = row.getAttribute("data-search") || "";
      var matchesText = !term || haystack.indexOf(term) !== -1;
      var matchesStatus = !status || row.getAttribute("data-status") === status;
      var matches = matchesText && matchesStatus;
      row.hidden = !matches;
      if (matches) visible += 1;
    });

    var emptyRow = $(".table-empty-row", table);
    if (!emptyRow) {
      emptyRow = document.createElement("tr");
      emptyRow.className = "table-empty-row";
      var cell = document.createElement("td");
      cell.colSpan = table.tHead ? table.tHead.rows[0].cells.length : 5;
      cell.textContent = "No rows match the current search and filter.";
      emptyRow.appendChild(cell);
      var body = table.tBodies[0];
      if (body) body.appendChild(emptyRow);
    }
    if (emptyRow && emptyRow.parentNode) emptyRow.hidden = visible > 0;

    counters.forEach(function (counter) {
      counter.textContent = visible + " shown";
    });
  }

  $$("[data-table-filter]").forEach(function (input) {
    var selector = input.getAttribute("data-table-filter");
    input.addEventListener("input", function () {
      applyTableFilter(selector);
    });
  });
  $$("[data-table-status]").forEach(function (select) {
    var selector = select.getAttribute("data-table-status");
    select.addEventListener("change", function () {
      applyTableFilter(selector);
    });
  });
  $$("[data-table-filter]").forEach(function (input) {
    applyTableFilter(input.getAttribute("data-table-filter"));
  });

  /* Flash messages as dismissible toasts -------------------------------- */
  var flashRegion = $("[data-flash-region]");
  if (flashRegion) {
    var toastRegion = $("[data-toast-region]");
    if (!toastRegion) {
      toastRegion = document.createElement("div");
      toastRegion.className = "toast-region";
      toastRegion.setAttribute("data-toast-region", "");
      toastRegion.setAttribute("aria-live", "polite");
      document.body.appendChild(toastRegion);
    }

    function dismissToast(element) {
      if (!element || element.classList.contains("is-leaving")) return;
      element.classList.add("is-leaving");
      window.setTimeout(function () {
        if (element.parentNode) element.parentNode.removeChild(element);
      }, 220);
    }

    $$("[data-flash]", flashRegion).forEach(function (flash) {
      flash.classList.add("toast");
      toastRegion.appendChild(flash);
      window.setTimeout(function () {
        dismissToast(flash);
      }, 7000);
    });

    document.addEventListener("click", function (event) {
      var close = event.target.closest ? event.target.closest("[data-flash-close]") : null;
      if (!close) return;
      dismissToast(close.closest(".flash"));
    });

    if (!flashRegion.children.length) flashRegion.parentNode.removeChild(flashRegion);
  }

  /* Submit lock ---------------------------------------------------------- */
  document.addEventListener("submit", function (event) {
    if (event.defaultPrevented) return;
    var form = event.target;
    if (!form || form.tagName !== "FORM" || !form.checkValidity()) return;
    $$("button[type='submit']", form).forEach(function (button) {
      button.classList.add("is-loading");
      button.disabled = true;
    });
  });

  /* Queue live region ---------------------------------------------------- */
  var queuePage = $("[data-queue-page]");
  var liveRegion = $("[data-live-region]");
  if (queuePage && liveRegion) {
    var pauseButton = $("[data-queue-pause]");
    var refreshButton = $("[data-queue-refresh]");
    var updatedLabel = $("[data-updated]");
    var liveState = $("[data-live-state]");
    var announce = $("[data-live-announce]");
    var paused = false;
    var loading = false;
    var lastUpdated = Date.now();
    var refreshSeconds = parseInt(document.body.getAttribute("data-refresh-seconds"), 10);
    var intervalMs = (refreshSeconds >= 5 ? refreshSeconds : 30) * 1000;

    function setUpdatedLabel() {
      if (!updatedLabel) return;
      var seconds = Math.max(0, Math.round((Date.now() - lastUpdated) / 1000));
      if (seconds < 5) {
        updatedLabel.textContent = "Updated just now";
      } else if (seconds < 60) {
        updatedLabel.textContent = "Updated " + seconds + "s ago";
      } else {
        updatedLabel.textContent = "Updated " + Math.floor(seconds / 60) + "m ago";
      }
    }

    function setPaused(value) {
      paused = value;
      queuePage.classList.toggle("is-paused", paused);
      if (liveState) liveState.classList.toggle("is-paused", paused);
      if (pauseButton) {
        pauseButton.setAttribute("aria-pressed", String(paused));
        pauseButton.innerHTML = paused
          ? '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg><span>Resume</span>'
          : '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg><span>Pause</span>';
      }
      if (announce) {
        announce.textContent = paused ? "Automatic queue refresh paused." : "Automatic queue refresh resumed.";
      }
    }

    function refreshQueue(manual) {
      if (loading) return;
      if (!manual && (paused || document.hidden)) return;
      loading = true;
      if (refreshButton) refreshButton.classList.add("is-loading");

      fetch(window.location.href, {
        cache: "no-store",
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" }
      })
        .then(function (response) {
          if (!response.ok) throw new Error("Queue refresh failed");
          return response.text();
        })
        .then(function (html) {
          var parsed = new DOMParser().parseFromString(html, "text/html");
          var next = $("[data-live-region]", parsed);
          if (!next) return;
          liveRegion.innerHTML = next.innerHTML;
          lastUpdated = Date.now();
          setUpdatedLabel();
          if (announce) announce.textContent = "Queue updated.";
        })
        .catch(function () {
          if (announce) announce.textContent = "Queue could not be refreshed. It will try again shortly.";
        })
        .finally(function () {
          loading = false;
          if (refreshButton) refreshButton.classList.remove("is-loading");
        });
    }

    if (pauseButton) {
      pauseButton.addEventListener("click", function () {
        setPaused(!paused);
      });
    }
    if (refreshButton) {
      refreshButton.addEventListener("click", function () {
        refreshQueue(true);
      });
    }

    setUpdatedLabel();
    window.setInterval(setUpdatedLabel, 1000);
    window.setInterval(function () {
      refreshQueue(false);
    }, intervalMs);

    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && !paused) refreshQueue(false);
    });
  }

  /* Full-screen queue ---------------------------------------------------- */
  var fullscreenButton = $("[data-fullscreen]");
  if (fullscreenButton) {
    var setFullscreen = function (active) {
      document.body.classList.toggle("queue-fullscreen", active);
      fullscreenButton.setAttribute("aria-pressed", String(active));
      fullscreenButton.innerHTML = active
        ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 3v3a2 2 0 0 1-2 2H3M21 8h-3a2 2 0 0 1-2-2V3M3 16h3a2 2 0 0 1 2 2v3M16 21v-3a2 2 0 0 1 2-2h3"/></svg><span>' +
          (active ? "Exit full screen" : "Full screen") +
          "</span>"
        : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3"/></svg><span>Full screen</span>';
    };

    fullscreenButton.addEventListener("click", function () {
      var active = !document.body.classList.contains("queue-fullscreen");
      setFullscreen(active);
      if (active && document.documentElement.requestFullscreen) {
        document.documentElement.requestFullscreen().catch(function () {});
      } else if (!active && document.fullscreenElement && document.exitFullscreen) {
        document.exitFullscreen().catch(function () {});
      }
    });

    document.addEventListener("fullscreenchange", function () {
      if (!document.fullscreenElement) setFullscreen(false);
    });
  }

  /* Appointment ledger detail dialog ------------------------------------ */
  var ledgerDialog = $("#ledger-detail-dialog");
  if (ledgerDialog) {
    var detailForm = $("#ledger-detail-form", ledgerDialog);
    var detailStatus = $("#ledger-detail-status", ledgerDialog);
    var detailAction = $("#ledger-detail-action", ledgerDialog);
    var statusBase = ledgerDialog.getAttribute("data-status-base") || "";
    var nextSteps = {
      BOOKED: ["WAITING", "Check in"],
      WAITING: ["IN_PROGRESS", "Start consultation"],
      IN_PROGRESS: ["COMPLETED", "Complete visit"]
    };

    $$("[data-ledger-detail]").forEach(function (button) {
      button.addEventListener("click", function () {
        var field = function (name) {
          return $("[data-detail='" + name + "']", ledgerDialog);
        };
        var id = button.getAttribute("data-appointment-id");
        var status = button.getAttribute("data-status") || "";
        var values = {
          id: id,
          patient: button.getAttribute("data-patient") || "—",
          phone: button.getAttribute("data-phone") || "—",
          doctor: button.getAttribute("data-doctor") || "—",
          department: button.getAttribute("data-department") || "—",
          date: button.getAttribute("data-date") || "—",
          time: button.getAttribute("data-time") || "—",
          token: button.getAttribute("data-token") || "—",
          reason: button.getAttribute("data-reason") || "—"
        };
        Object.keys(values).forEach(function (key) {
          var node = field(key);
          if (node) node.textContent = values[key];
        });

        var badge = field("status-badge");
        if (badge) {
          badge.textContent = button.getAttribute("data-status-label") || status;
          badge.setAttribute("data-status", status);
        }

        var step = nextSteps[status];
        if (detailForm && detailStatus && detailAction) {
          detailForm.action = statusBase.replace("/0/", "/" + id + "/");
          if (step && button.getAttribute("data-can-manage") === "1") {
            detailForm.hidden = false;
            detailStatus.value = step[0];
            detailAction.textContent = step[1];
          } else {
            detailForm.hidden = true;
          }
        }
        openDialog(ledgerDialog);
      });
    });
  }

  /* Escape closes navigation and search --------------------------------- */
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      setMenu(false);
      document.body.classList.remove("search-open");
      if (searchToggle) searchToggle.setAttribute("aria-expanded", "false");
    }
  });
})();
