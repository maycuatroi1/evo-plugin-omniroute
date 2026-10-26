// The page of lumen-hello. It reaches the hub only through window.evo (the hub's UI kit 1 and the bridge
// evo-plugin/1): evo.route calls the plugin's own route, /v1/x/lumen-hello/<project>/waves, with the viewer's session.
// Its words come from i18n/<locale>.js beside this page, which the kit loads; the kit keeps the theme and the frame's
// height.
(function () {
  "use strict";
  var evo = window.evo;

  function show(board) {
    document.getElementById("greeting").textContent = evo.t("greeting", { greeting: board.greeting });
    var body = document.querySelector("#waves tbody");
    body.textContent = "";
    board.waves.forEach(function (wave) {
      var row = document.createElement("tr");
      var name = document.createElement("td");
      name.textContent = wave.name;
      var when = document.createElement("td");
      when.className = "evo-hide-sm";
      when.textContent = new Date(wave.at).toLocaleString(document.documentElement.lang || "en");
      row.appendChild(name);
      row.appendChild(when);
      body.appendChild(row);
    });
    document.getElementById("empty").hidden = board.waves.length > 0;
  }

  function failed(error) {
    evo.toast(evo.t("failed", { message: (error && error.message) || String(error) }), "error");
  }

  evo.ready
    .then(function (context) {
      document.querySelectorAll("[data-i18n]").forEach(function (node) {
        node.textContent = evo.t(node.getAttribute("data-i18n"));
      });
      var input = document.getElementById("name");
      input.placeholder = evo.t("name");
      input.value = (context.viewer && context.viewer.login) || "";
      return evo.route("waves");
    })
    .then(function (answer) {
      show(answer.body);
    })
    .catch(failed);

  document.getElementById("wave").addEventListener("submit", function (event) {
    event.preventDefault();
    var name = document.getElementById("name").value.trim();
    if (!name) return;
    evo
      .route("waves", { method: "POST", body: { name: name } })
      .then(function (answer) {
        if (answer.status >= 400) throw new Error(answer.body.detail || String(answer.status));
        show(answer.body);
        evo.toast(evo.t("sent"), "success");
      })
      .catch(failed);
  });
})();
