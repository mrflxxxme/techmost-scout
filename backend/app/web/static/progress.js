// Живой прогресс поиска (SPEC Э3-4): ванильный EventSource, без
// htmx-SSE-расширения (РЕШ-189 п. 9). Один контейнер — один поток; поток
// закрывается сам на событии "done", страница перезагружается, чтобы хаб
// показал ссылку на готовый отчёт.
(function () {
  "use strict";

  function renderLine(container, data) {
    var line = document.createElement("div");
    line.className = "progress-line" + (data.stage === "done" ? " done" : "");
    var label = document.createElement("span");
    label.className = "stage-label";
    label.textContent = (data.keepalive ? "· " : "") + data.message;
    var fraction = document.createElement("span");
    fraction.className = "stage-fraction";
    fraction.textContent = Math.round((data.fraction || 0) * 100) + "%";
    line.appendChild(label);
    line.appendChild(fraction);
    container.appendChild(line);
    container.scrollTop = container.scrollHeight;
  }

  function connect(container) {
    var source = new EventSource("/requests/" + container.dataset.requestId + "/progress");
    source.onmessage = function (event) {
      var data = JSON.parse(event.data);
      renderLine(container, data);
      if (data.stage === "done" || data.fraction >= 1) {
        source.close();
        window.setTimeout(function () {
          window.location.reload();
        }, 800);
      }
    };
    source.onerror = function () {
      source.close();
    };
  }

  document.querySelectorAll("[data-progress-log]").forEach(connect);
})();
