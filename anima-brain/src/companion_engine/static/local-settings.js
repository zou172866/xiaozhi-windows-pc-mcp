"use strict";
const form = document.getElementById("settings-form");
const status = document.getElementById("status");
const fragment = new URLSearchParams(location.hash.slice(1));
const token = fragment.get("token") || sessionStorage.getItem("companion-settings-token");
if (fragment.has("token")) {
  sessionStorage.setItem("companion-settings-token", token);
  history.replaceState(null, "", location.pathname);
}
let initial = {};
const groups = [
  ["Reply model", ["base_url", "model", "api_key"]],
  ["Vision", ["vision_base_url", "vision_model", "vision_key"]],
  ["Memory summaries", ["summary_base_url", "summary_model", "summary_key"]],
  ["Embeddings", ["embedding_base_url", "embedding_model", "embedding_key"]],
  ["Image generation", ["image_base_url", "image_model", "image_key"]],
  ["Speech", ["speech_provider", "speech_model", "speech_key"]],
  ["Transcription", ["transcription_base_url", "transcription_model", "transcription_key"]],
  ["Generation limits", ["reader_enabled", "reader_max_steps", "max_output_tokens",
    "vision_max_output_tokens", "timeout_seconds", "media_timeout_seconds", "temperature"]],
];
const numbers = new Set(groups.at(-1)[1].filter(name => name !== "reader_enabled"));
function node(tag, text) {
  const element = document.createElement(tag);
  if (text) element.textContent = text;
  return element;
}
function message(text, error = false) {
  status.textContent = text;
  status.dataset.error = String(error);
}
async function api(body) {
  if (!token) throw new Error("Open settings from Companion Engine on this computer.");
  const response = await fetch("/api/settings", {
    method: body ? "POST" : "GET",
    headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Settings request failed.");
  return data;
}
function render(settings) {
  initial = settings;
  const fields = document.getElementById("fields");
  const opened = new Set([...fields.querySelectorAll("details[open]")].map(el => el.dataset.title));
  fields.replaceChildren();
  for (const [title, names] of groups) {
    const section = node("details");
    section.dataset.title = title;
    section.open = opened.has(title) || title === "Reply model";
    section.append(node("summary", title));
    for (const name of names) {
      const secret = name.endsWith("key");
      const label = node("label", name.replaceAll("_", " "));
      let input = node(name === "speech_provider" ? "select" : "input");
      if (name === "speech_provider") {
        for (const value of ["elevenlabs", "minimax", "doubao"]) {
          const option = node("option", value);
          option.value = value;
          input.append(option);
        }
      } else {
        input.type = secret ? "password" : name === "reader_enabled" ? "checkbox"
          : numbers.has(name) ? "number" : "text";
        input.autocomplete = "off";
        if (numbers.has(name)) input.step = "any";
      }
      input.name = name;
      if (secret) {
        input.placeholder = settings[name] ? "Configured; leave blank to keep" : "Not configured";
      } else if (name === "reader_enabled") input.checked = settings[name];
      else input.value = settings[name] ?? "";
      label.append(input);
      section.append(label);
      if (secret) {
        const clearLabel = node("label");
        clearLabel.className = "clear";
        const clear = node("input");
        clear.type = "checkbox";
        clear.dataset.clear = name;
        clear.addEventListener("change", () => { input.disabled = clear.checked; });
        clearLabel.append(clear, document.createTextNode("Clear this key when applying or saving"));
        section.append(clearLabel);
      }
    }
    fields.append(section);
  }
  form.hidden = false;
}
form.addEventListener("submit", async event => {
  event.preventDefault();
  const settings = {}, clear_keys = [];
  for (const input of form.querySelectorAll("[name]")) {
    if (input.disabled) continue;
    const name = input.name;
    let value = input.type === "checkbox" ? input.checked : input.value.trim();
    if (name.endsWith("key")) { if (value) settings[name] = value; continue; }
    if (numbers.has(name)) value = value === "" && name === "temperature" ? null : Number(value);
    if (value !== initial[name]) settings[name] = value;
  }
  for (const input of form.querySelectorAll("[data-clear]:checked")) clear_keys.push(input.dataset.clear);
  form.inert = true;
  try {
    const result = await api({ settings, clear_keys, persist: event.submitter?.dataset.persist === "true" });
    render(result.settings);
    message(result.persisted ? "Saved for future starts and applied to this process." : "Applied to this process only.");
  } catch (error) { message(error.message, true); }
  finally { form.inert = false; }
});
api().then(data => {
  document.getElementById("storage").textContent = "Saved credential storage: " +
    data.credential_storage.saved_secrets + ". Secret values are never returned to this page.";
  render(data.settings);
  message("Ready. Only changed fields will be applied.");
}).catch(error => message(error.message, true));
