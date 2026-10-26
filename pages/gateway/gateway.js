// The page of omniroute. It reaches the hub only through window.evo (the hub's UI kit 1 and the bridge evo-plugin/1):
// evo.route calls the plugin's own routes, /v1/x/omniroute/<project>/status, models and ask, with the viewer's
// session; the backend calls the gateway, so the page never holds the key. Its words come from i18n/<locale>.js beside
// this page, which the kit loads; the kit keeps the theme and the frame's height.
(function () {
  "use strict";
  var evo = window.evo;
  var $ = function (id) {
    return document.getElementById(id);
  };

  function failure(answer) {
    var body = (answer && answer.body) || {};
    return new Error(body.detail || evo.t("status_code", { status: answer && answer.status }));
  }

  function checked(answer) {
    if (!answer || answer.status >= 400) throw failure(answer);
    return answer.body;
  }

  function failed(error) {
    evo.toast(evo.t("failed", { message: (error && error.message) || String(error) }), "error");
  }

  function compact(number) {
    if (typeof number !== "number") return "-";
    if (number >= 1000000) return (number / 1000000).toFixed(1).replace(/\.0$/, "") + "M";
    if (number >= 1000) return (number / 1000).toFixed(1).replace(/\.0$/, "") + "k";
    return String(number);
  }

  function money(value) {
    return typeof value === "number" ? "$" + value.toFixed(2) : "-";
  }

  function usageWords(usage) {
    if (!usage || !usage.allowed) return evo.t("usage_off");
    var parts = [];
    var personal = usage.personal;
    if (personal) {
      parts.push(evo.t("usage_today", { spent: money(personal.dailySpentUsd), limit: money(personal.dailyLimitUsd) }));
      parts.push(evo.t("usage_week", { spent: money(personal.weeklySpentUsd), limit: money(personal.weeklyLimitUsd) }));
    } else {
      parts.push(evo.t("usage_unlimited"));
    }
    var providers = (usage.providers || []).map(function (item) {
      return item.provider;
    });
    parts.push(
      providers.length
        ? evo.t("usage_providers", { count: providers.length, names: providers.join(", ") })
        : evo.t("usage_no_providers")
    );
    return parts.join(" – ");
  }

  function setState(kind, words) {
    var badge = $("state");
    badge.className = "evo-badge evo-badge-" + kind;
    badge.textContent = words;
  }

  function showStatus(status) {
    var ok = status.configured && !status.error;
    if (!status.configured) setState("warning", evo.t("state_unset"));
    else if (ok) setState("success", evo.t("state_ok"));
    else setState("danger", status.reachable ? evo.t("state_refused") : evo.t("state_down"));

    $("problem").hidden = ok;
    $("problem").textContent = !status.configured ? evo.t("unset_help") : status.error || "";
    $("facts").hidden = !status.configured;
    $("url").textContent = status.base_url || "-";
    $("url").title = status.base_url || "";
    $("latency").textContent = typeof status.latency_ms === "number" ? status.latency_ms + " ms" : "-";
    $("models-count").textContent = typeof status.models === "number" ? String(status.models) : "-";
    $("default-model").textContent = status.default_model || "-";
    $("model").placeholder = status.default_model || "auto";
    $("usage-field").hidden = !ok;
    $("usage").textContent = usageWords(status.usage);
    $("ask-card").hidden = !ok || !status.can_ask;
    $("models-card").hidden = !ok;
    return ok;
  }

  function loadStatus() {
    var button = $("refresh");
    button.disabled = true;
    return evo
      .route("status")
      .then(checked)
      .then(function (status) {
        if (showStatus(status)) return loadModels();
      })
      .catch(failed)
      .then(function () {
        button.disabled = false;
      });
  }

  function showModels(listed) {
    var body = document.querySelector("#models tbody");
    var options = $("model-ids");
    body.textContent = "";
    options.textContent = "";
    listed.models.forEach(function (row) {
      var tr = document.createElement("tr");
      var id = document.createElement("td");
      id.className = "evo-mono evo-wrap";
      id.textContent = row.id;
      var context = document.createElement("td");
      context.className = "evo-hide-sm";
      context.textContent = compact(row.context_length);
      var abilities = document.createElement("td");
      abilities.className = "evo-hide-sm";
      row.capabilities.forEach(function (name) {
        var badge = document.createElement("span");
        badge.className = "evo-badge evo-badge-outline";
        badge.textContent = evo.t(name);
        abilities.appendChild(badge);
        abilities.appendChild(document.createTextNode(" "));
      });
      tr.appendChild(id);
      tr.appendChild(context);
      tr.appendChild(abilities);
      body.appendChild(tr);
      var option = document.createElement("option");
      option.value = row.id;
      options.appendChild(option);
    });
    $("models-empty").hidden = listed.models.length > 0;
    $("models-total").textContent = evo.t("models_shown", { shown: listed.models.length, total: listed.total });
  }

  function loadModels() {
    var query = { limit: "50" };
    var word = $("q").value.trim();
    var capability = $("capability").value;
    if (word) query.q = word;
    if (capability) query.capability = capability;
    return evo.route("models", { query: query }).then(checked).then(showModels);
  }

  function ask(event) {
    event.preventDefault();
    var prompt = $("prompt").value.trim();
    if (!prompt) return;
    var send = $("send");
    send.disabled = true;
    send.textContent = evo.t("sending");
    var body = { prompt: prompt };
    var model = $("model").value.trim();
    if (model) body.model = model;
    evo
      .route("ask", { method: "POST", body: body })
      .then(checked)
      .then(function (answer) {
        var usage = answer.usage || {};
        $("reply").hidden = false;
        $("reply-text").textContent = answer.content.trim() || evo.t("no_text", { reason: answer.finish_reason || "-" });
        $("reply-meta").textContent = evo.t("reply_meta", {
          model: answer.model,
          input: usage.prompt_tokens == null ? "-" : usage.prompt_tokens,
          output: usage.completion_tokens == null ? "-" : usage.completion_tokens,
          ms: answer.latency_ms,
        });
      })
      .catch(failed)
      .then(function () {
        send.disabled = false;
        send.textContent = evo.t("send");
      });
  }

  evo.ready
    .then(function () {
      document.querySelectorAll("[data-i18n]").forEach(function (node) {
        node.textContent = evo.t(node.getAttribute("data-i18n"));
      });
      $("q").placeholder = evo.t("search_hint");
      $("prompt").placeholder = evo.t("prompt_hint");
      setState("outline", evo.t("state_loading"));
      return loadStatus();
    })
    .catch(failed);

  $("refresh").addEventListener("click", loadStatus);
  $("ask").addEventListener("submit", ask);
  $("filter").addEventListener("submit", function (event) {
    event.preventDefault();
    loadModels().catch(failed);
  });
})();
