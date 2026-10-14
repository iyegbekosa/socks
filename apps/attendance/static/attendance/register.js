// Register page: tap a student's name to cycle present -> absent -> late -> excused, and keep
// the sticky counts current. The page works without this script (plain radio buttons).
(function () {
  "use strict";
  var form = document.getElementById("reg");
  if (!form) return;
  var keys = ["present", "absent", "late", "excused"];

  function recount() {
    var c = { present: 0, absent: 0, late: 0, excused: 0 };
    form.querySelectorAll("input[type=radio]:checked").forEach(function (i) {
      c[i.value] += 1;
    });
    keys.forEach(function (k) {
      var el = document.getElementById("c-" + k);
      if (el) el.textContent = c[k];
    });
  }

  function cycle(name) {
    var radios = name.parentNode.querySelectorAll("input[type=radio]");
    if (!radios.length) return;
    var i = 0;
    while (i < radios.length && !radios[i].checked) i += 1;
    radios[i >= radios.length ? 0 : (i + 1) % radios.length].checked = true;
    recount();
  }

  form.querySelectorAll(".n").forEach(function (n) {
    if (!n.parentNode.querySelector("input[type=radio]")) return;
    n.setAttribute("role", "button");
    n.setAttribute("tabindex", "0");
  });
  form.addEventListener("click", function (e) {
    var n = e.target.closest(".n[role=button]");
    if (n) cycle(n);
  });
  form.addEventListener("keydown", function (e) {
    var n = e.target.closest && e.target.closest(".n[role=button]");
    if (n && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      cycle(n);
    }
  });
  form.addEventListener("change", recount);
})();
