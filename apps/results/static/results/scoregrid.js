(function () {
  "use strict";
  var root = document.getElementById("scoregrid");
  if (!root || !root.dataset.apiUrl) return;
  var tokenEl = root.querySelector("input[name=csrfmiddlewaretoken]");
  var csrf = tokenEl ? tokenEl.value : "";
  var banner = document.getElementById("banner");
  var url = root.dataset.apiUrl + "?offering=" + encodeURIComponent(root.dataset.offering) +
    "&term=" + encodeURIComponent(root.dataset.term);
  var inputs = Array.prototype.slice.call(root.querySelectorAll("input.cell"));

  function rowOf(el) { return el.closest("tr"); }
  function rowInputs(tr) { return Array.prototype.slice.call(tr.querySelectorAll("input.cell")); }
  function find(student, component) {
    for (var i = 0; i < inputs.length; i++) {
      if (inputs[i].dataset.student === String(student) && inputs[i].dataset.component === String(component)) return inputs[i];
    }
    return null;
  }
  function note(el, msg) {
    var m = el.parentNode.querySelector(".cellmsg");
    if (!msg) {
      if (m) m.remove();
      el.removeAttribute("aria-invalid");
      el.classList.remove("conflict");
      return;
    }
    if (!m) {
      m = document.createElement("span");
      m.className = "cellmsg";
      m.setAttribute("role", "alert");
      el.parentNode.appendChild(m);
    }
    m.textContent = msg;
    el.setAttribute("aria-invalid", "true");
  }
  function showBanner(msg) {
    banner.textContent = msg;
    banner.hidden = false;
  }
  function lock(msg) {
    showBanner(msg);
    inputs.forEach(function (i) { i.disabled = true; });
  }
  function total(tr) {
    var sum = 0, any = false;
    rowInputs(tr).forEach(function (i) {
      var v = parseFloat(i.value);
      if (!isNaN(v)) { sum += v; any = true; }
    });
    tr.querySelector("[data-total]").textContent = any ? sum.toFixed(1) : "";
  }
  function dirty(el) { return el.value.trim() !== el.dataset.saved; }
  function mark(tr) {
    var u = tr.querySelector("[data-unsaved]");
    if (u) u.hidden = !rowInputs(tr).some(dirty);
  }
  function errorOf(body) { return (body && body.error) || {}; }

  function save(tr) {
    var changed = rowInputs(tr).filter(dirty);
    if (!changed.length) return;
    var cells = changed.map(function (i) {
      return { student: +i.dataset.student, component: +i.dataset.component,
        value: i.value.trim() === "" ? null : i.value.trim(), version: +i.dataset.version };
    });
    fetch(url, {
      method: "PUT",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf, "Accept": "application/json" },
      body: JSON.stringify({ cells: cells })
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (b) { return { status: r.status, body: b }; });
    }).then(function (res) {
      var err = errorOf(res.body), details = err.details || [];
      if (res.status === 200) {
        (res.body.versions || []).forEach(function (v) {
          var el = find(v.student, v.component);
          if (!el) return;
          el.dataset.version = v.version;
          el.dataset.saved = el.value.trim();
          note(el, "");
        });
        mark(tr);
      } else if (res.status === 409 && err.code === "VERSION_CONFLICT") {
        details.forEach(function (d) {
          var el = find(d.student, d.component);
          if (!el) return;
          el.dataset.version = d.version;
          note(el, "Changed by someone else: now " + (d.value === null ? "blank" : d.value));
          el.classList.add("conflict");
          el.dataset.saved = d.value === null ? "" : d.value;
        });
      } else if (res.status === 409 && err.code === "RESULTS_LOCKED") {
        lock(err.message || "These scores are locked.");
      } else if (res.status === 400) {
        details.forEach(function (d) {
          var p = String(d.field).split(":"), el = p.length === 2 ? find(p[0], p[1]) : null;
          if (el) note(el, d.message); else showBanner(d.message);
        });
      } else {
        showBanner("Could not save. Check your connection and try again.");
      }
      mark(tr);
    }).catch(function () {
      showBanner("Could not save. Check your connection and try again.");
    });
  }

  inputs.forEach(function (el) {
    el.addEventListener("input", function () {
      var tr = rowOf(el);
      note(el, "");
      total(tr);
      mark(tr);
    });
    el.addEventListener("change", function () { save(rowOf(el)); });
    el.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" && e.key !== "ArrowDown") return;
      e.preventDefault();
      var tr = rowOf(el), idx = rowInputs(tr).indexOf(el), next = tr.nextElementSibling;
      while (next && !next.querySelector("input.cell")) next = next.nextElementSibling;
      if (next) rowInputs(next)[idx].focus();
      else el.blur();
    });
  });
  Array.prototype.forEach.call(root.querySelectorAll("tr[data-row]"), total);
})();
