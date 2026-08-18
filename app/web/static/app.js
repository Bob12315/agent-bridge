document.addEventListener("DOMContentLoaded", () => {
  const panel = document.querySelector("#session-panel[data-session-id]");
  if (!panel || !window.EventSource) return;

  const sessionId = panel.dataset.sessionId;
  const source = new EventSource(`/api/events?session_id=${encodeURIComponent(sessionId)}`);
  source.onmessage = () => window.htmx?.trigger(panel, "refresh");
  ["SESSION_CREATED", "MESSAGE_RECEIVED", "MESSAGE_ROUTED", "AGENT_STARTED",
   "AGENT_PROGRESS", "AGENT_FINISHED", "AGENT_FAILED", "REQUEST_CANCELLED",
   "SESSION_CLOSED"].forEach((name) => {
    source.addEventListener(name, () => {
      if (window.htmx) window.htmx.ajax("GET", `/sessions/${sessionId}/panel`, {target: panel, swap: "innerHTML"});
    });
  });
});
