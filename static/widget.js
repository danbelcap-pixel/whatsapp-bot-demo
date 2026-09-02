/*
 * Widget de chat embebible — Beltrán Servicios Digitales.
 *
 * Uso en la página del cliente:
 *   <script src="https://TU-SERVIDOR.onrender.com/static/widget.js"
 *           data-widget-id="EL_WIDGET_ID_DEL_NEGOCIO"></script>
 *
 * Reutiliza el mismo backend/cerebro que el bot de WhatsApp (ver main.py,
 * rutas /widget/message y /widget/history) — este archivo es solo la
 * interfaz visual, toda la lógica de negocio vive en el servidor.
 */
(function () {
  "use strict";

  var scriptTag = document.currentScript;
  var widgetId = scriptTag.getAttribute("data-widget-id");
  if (!widgetId) {
    console.error("[widget] Falta data-widget-id en el <script> del chat.");
    return;
  }

  var apiBase = new URL(scriptTag.src).origin;
  var accentColor = scriptTag.getAttribute("data-color") || "#1f8a4c";
  var storageKey = "bsd_widget_visitor_id";

  function getVisitorId() {
    try {
      var id = localStorage.getItem(storageKey);
      if (!id) {
        id = "v" + Date.now().toString(36) + Math.random().toString(36).slice(2);
        localStorage.setItem(storageKey, id);
      }
      return id;
    } catch (e) {
      // Si el navegador bloquea localStorage (modo privado estricto, etc.),
      // se usa un id de solo esta sesión — no persiste, pero no rompe nada.
      return "v" + Date.now().toString(36) + Math.random().toString(36).slice(2);
    }
  }

  var visitorId = getVisitorId();

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  // ── Estilos ──────────────────────────────────────────────────────────
  var style = document.createElement("style");
  style.textContent = [
    "#bsd-widget-bubble{position:fixed;bottom:20px;right:20px;width:56px;height:56px;",
    "border-radius:50%;background:" + accentColor + ";box-shadow:0 4px 14px rgba(0,0,0,.25);",
    "cursor:pointer;display:flex;align-items:center;justify-content:center;z-index:999999;",
    "transition:transform .15s ease;}",
    "#bsd-widget-bubble:hover{transform:scale(1.06);}",
    "#bsd-widget-bubble svg{width:26px;height:26px;fill:#fff;}",
    "#bsd-widget-panel{position:fixed;bottom:88px;right:20px;width:340px;max-width:92vw;",
    "height:460px;max-height:75vh;background:#fff;border-radius:14px;",
    "box-shadow:0 8px 30px rgba(0,0,0,.22);display:none;flex-direction:column;",
    "overflow:hidden;z-index:999999;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;}",
    "#bsd-widget-panel.open{display:flex;}",
    "#bsd-widget-header{background:" + accentColor + ";color:#fff;padding:14px 16px;",
    "font-weight:600;font-size:15px;display:flex;justify-content:space-between;align-items:center;}",
    "#bsd-widget-close{cursor:pointer;opacity:.85;font-size:18px;line-height:1;}",
    "#bsd-widget-close:hover{opacity:1;}",
    "#bsd-widget-messages{flex:1;overflow-y:auto;padding:14px;background:#f7f7f8;}",
    ".bsd-msg{max-width:82%;padding:9px 12px;border-radius:12px;margin-bottom:8px;",
    "font-size:13.5px;line-height:1.4;white-space:pre-wrap;word-wrap:break-word;}",
    ".bsd-msg.user{background:" + accentColor + ";color:#fff;margin-left:auto;",
    "border-bottom-right-radius:3px;}",
    ".bsd-msg.assistant{background:#ececef;color:#222;margin-right:auto;",
    "border-bottom-left-radius:3px;}",
    "#bsd-widget-inputbar{display:flex;border-top:1px solid #e7e7e9;padding:8px;background:#fff;}",
    "#bsd-widget-input{flex:1;border:none;outline:none;font-size:13.5px;padding:8px 10px;",
    "resize:none;font-family:inherit;}",
    "#bsd-widget-send{background:" + accentColor + ";color:#fff;border:none;border-radius:8px;",
    "padding:0 14px;font-size:13px;cursor:pointer;margin-left:6px;}",
    "#bsd-widget-send:disabled{opacity:.5;cursor:default;}",
    ".bsd-msg.typing{color:#888;font-style:italic;}",
  ].join("");
  document.head.appendChild(style);

  // ── Estructura ───────────────────────────────────────────────────────
  var bubble = document.createElement("div");
  bubble.id = "bsd-widget-bubble";
  bubble.innerHTML = '<svg viewBox="0 0 24 24"><path d="M4 4h16v12H7l-3 3V4z"/></svg>';
  document.body.appendChild(bubble);

  var panel = document.createElement("div");
  panel.id = "bsd-widget-panel";
  panel.innerHTML =
    '<div id="bsd-widget-header"><span>¿En qué te ayudamos?</span>' +
    '<span id="bsd-widget-close">&times;</span></div>' +
    '<div id="bsd-widget-messages"></div>' +
    '<div id="bsd-widget-inputbar">' +
    '<textarea id="bsd-widget-input" rows="1" placeholder="Escribe tu mensaje..."></textarea>' +
    '<button id="bsd-widget-send">Enviar</button>' +
    "</div>";
  document.body.appendChild(panel);

  var messagesEl = panel.querySelector("#bsd-widget-messages");
  var inputEl = panel.querySelector("#bsd-widget-input");
  var sendBtn = panel.querySelector("#bsd-widget-send");
  var closeBtn = panel.querySelector("#bsd-widget-close");

  var historyLoaded = false;
  var renderedCount = 0; // cuántos mensajes del historial del servidor ya se muestran
  var pollTimer = null;
  var POLL_INTERVAL_MS = 4000;

  function appendMessage(role, text) {
    var div = document.createElement("div");
    div.className = "bsd-msg " + (role === "user" ? "user" : "assistant");
    div.innerHTML = escapeHtml(text);
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
  }

  function loadHistory() {
    if (historyLoaded) return;
    historyLoaded = true;
    var url = apiBase + "/widget/history?widget_id=" + encodeURIComponent(widgetId) +
      "&visitor_id=" + encodeURIComponent(visitorId);
    fetch(url)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var mensajes = data.messages || [];
        mensajes.forEach(function (m) {
          appendMessage(m.role, m.text);
        });
        renderedCount = mensajes.length;
        if (mensajes.length === 0) {
          appendMessage("assistant", "¡Hola! 👋 ¿En qué te puedo ayudar?");
        }
      })
      .catch(function () {
        appendMessage("assistant", "¡Hola! 👋 ¿En qué te puedo ayudar?");
      });
  }

  function checkForUpdates() {
    // Mientras el chat esté abierto, revisa cada pocos segundos si llegó
    // algo nuevo al historial (por ejemplo, la respuesta del dueño del
    // negocio aprobando/rechazando una cita) — así el cliente no tiene que
    // cerrar y volver a abrir el chat para verla.
    var url = apiBase + "/widget/history?widget_id=" + encodeURIComponent(widgetId) +
      "&visitor_id=" + encodeURIComponent(visitorId);
    fetch(url)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var mensajes = data.messages || [];
        if (mensajes.length > renderedCount) {
          mensajes.slice(renderedCount).forEach(function (m) {
            appendMessage(m.role, m.text);
          });
          renderedCount = mensajes.length;
        }
      })
      .catch(function () {
        // Un fallo puntual de red al revisar no debe interrumpir nada —
        // simplemente se vuelve a intentar en el siguiente ciclo.
      });
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(checkForUpdates, POLL_INTERVAL_MS);
  }

  function stopPolling() {
    clearInterval(pollTimer);
    pollTimer = null;
  }

  function sendMessage() {
    var text = inputEl.value.trim();
    if (!text) return;
    appendMessage("user", text);
    inputEl.value = "";
    inputEl.style.height = "auto";
    sendBtn.disabled = true;
    var typingEl = appendMessage("assistant", "Escribiendo...");
    typingEl.classList.add("typing");

    fetch(apiBase + "/widget/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ widget_id: widgetId, visitor_id: visitorId, message: text }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        typingEl.remove();
        appendMessage("assistant", data.reply || "No pude procesar tu mensaje, intenta de nuevo.");
        // El servidor guardó exactamente 2 entradas visibles por este
        // intercambio (el mensaje del cliente y la respuesta) — se suma
        // aquí para que el siguiente sondeo no las vuelva a mostrar.
        renderedCount += 2;
      })
      .catch(function () {
        typingEl.remove();
        appendMessage("assistant", "Hubo un problema de conexión — intenta de nuevo en un momento.");
      })
      .finally(function () {
        sendBtn.disabled = false;
      });
  }

  bubble.addEventListener("click", function () {
    panel.classList.toggle("open");
    if (panel.classList.contains("open")) {
      loadHistory();
      startPolling();
      inputEl.focus();
    } else {
      stopPolling();
    }
  });
  closeBtn.addEventListener("click", function () {
    panel.classList.remove("open");
    stopPolling();
  });
  sendBtn.addEventListener("click", sendMessage);
  inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  inputEl.addEventListener("input", function () {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 90) + "px";
  });
})();
