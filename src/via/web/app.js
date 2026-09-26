// Via web UI: account, device and contact management, and sending.
// No build step and no dependencies. All DOM is built with textContent (never innerHTML),
// so names and other user data can't inject markup.

const $app = document.getElementById("app");
const $toasts = document.getElementById("toasts");

// ---- storage & API -----------------------------------------------------------------------

const TOKEN_KEY = "via.session";
const store = {
  get token() {
    try {
      return localStorage.getItem(TOKEN_KEY);
    } catch {
      return null;
    }
  },
  set token(value) {
    try {
      if (value) localStorage.setItem(TOKEN_KEY, value);
      else localStorage.removeItem(TOKEN_KEY);
    } catch {
      /* storage unavailable: the session lasts until the page is closed */
    }
  },
};
let memoryToken = null;
const token = () => store.token ?? memoryToken;

const state = { info: null, user: null };

class ApiError extends Error {
  constructor(message, code, status) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

function handle(status, data) {
  if (status === 401 && token()) {
    forgetSession();
    render();
  }
  if (status >= 400) {
    throw new ApiError(data?.error?.message || `Request failed (${status})`, data?.error?.code, status);
  }
  return data;
}

async function api(method, path, body) {
  const headers = {};
  if (token()) headers.Authorization = `Bearer ${token()}`;
  let payload;
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, { method, headers, body: payload });
  } catch {
    throw new ApiError("Can't reach the server", "network", 0);
  }
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  return handle(res.status, data);
}

function upload(path, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    xhr.setRequestHeader("Authorization", `Bearer ${token()}`);
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      let data = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        /* empty or non-JSON body */
      }
      try {
        resolve(handle(xhr.status, data));
      } catch (err) {
        reject(err);
      }
    };
    xhr.onerror = () => reject(new ApiError("Upload failed", "network", 0));
    xhr.send(file);
  });
}

function saveSession(value) {
  store.token = value;
  memoryToken = value;
}

function forgetSession() {
  store.token = null;
  memoryToken = null;
  state.user = null;
}

async function signOut() {
  try {
    await api("POST", "/v1/auth/logout");
  } catch {
    /* the session may already be gone */
  }
  forgetSession();
  location.hash = "";
  render();
}

// ---- DOM helpers -------------------------------------------------------------------------

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className = value;
    else if (key === "value" || key === "checked") el[key] = value;
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child != null && child !== false) el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

const field = (label, control, hint) =>
  h("label", { class: "field" }, h("span", { class: "field-label" }, label), control, hint && h("span", { class: "hint" }, hint));

const check = (label, input, disabled) =>
  h("label", { class: disabled ? "check disabled" : "check" }, input, h("span", {}, label));

const card = (...children) => h("section", { class: "card" }, ...children);

const pageHead = (title, subtitle) =>
  h("div", { class: "page-head" }, h("h1", {}, title), subtitle && h("p", { class: "muted" }, subtitle));

const empty = (text) => h("p", { class: "empty" }, text);

function busy(button, on) {
  button.disabled = on;
  button.classList.toggle("loading", on);
}

function toast(message, kind = "ok") {
  const el = h("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status" }, message);
  $toasts.append(el);
  setTimeout(() => el.remove(), kind === "error" ? 6000 : 3500);
}

// Run an action from a button: show progress, report errors as toasts.
async function act(button, fn) {
  busy(button, true);
  try {
    return await fn();
  } catch (err) {
    toast(err.message, "error");
  } finally {
    if (button.isConnected) busy(button, false);
  }
}

function copyButton(text) {
  const button = h("button", { class: "btn small", type: "button" }, "Copy");
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "Copied";
      setTimeout(() => (button.textContent = "Copy"), 1500);
    } catch {
      toast("Couldn't copy; select the text instead", "error");
    }
  });
  return button;
}

function secretPanel(title, secret, ...extra) {
  return h(
    "div",
    { class: "secret" },
    h("div", {}, h("strong", {}, title), " ", h("span", { class: "muted" }, "Shown only once. Copy it now.")),
    h("div", { class: "secret-row" }, h("code", {}, secret), copyButton(secret)),
    ...extra,
  );
}

// ---- formatting --------------------------------------------------------------------------

const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
const UNITS = [
  ["year", 31536000],
  ["month", 2592000],
  ["week", 604800],
  ["day", 86400],
  ["hour", 3600],
  ["minute", 60],
];

function ago(iso) {
  if (!iso) return "never";
  const seconds = (new Date(iso).getTime() - Date.now()) / 1000;
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size) return rtf.format(Math.round(seconds / size), unit);
  }
  return seconds > 0 ? "in a moment" : "just now";
}

const time = (iso) => h("time", { datetime: iso, title: iso ? new Date(iso).toLocaleString() : null }, ago(iso));

function bytes(n) {
  if (n < 1024) return `${n} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = n / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i++;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[i]}`;
}

function duration(seconds) {
  for (const [unit, size] of [["day", 86400], ["hour", 3600], ["minute", 60]]) {
    if (seconds >= size && seconds % size === 0) {
      const n = seconds / size;
      return `${n} ${unit}${n === 1 ? "" : "s"}`;
    }
  }
  return `${seconds} s`;
}

const DEVICE_TYPES = ["android", "ios", "desktop", "browser", "cli", "other"];
const SHARE_BY_DEFAULT = ["android", "ios", "desktop"];

// ---- app shell & routing -----------------------------------------------------------------

const NAV = [
  ["send", "Send"],
  ["sent", "Sent"],
  ["devices", "Devices"],
  ["contacts", "Contacts"],
  ["tokens", "App tokens"],
  ["account", "Account"],
];

const VIEWS = {
  send: viewSend,
  sent: viewSent,
  devices: viewDevices,
  contacts: viewContacts,
  tokens: viewTokens,
  account: viewAccount,
  admin: viewAdmin,
};

const brand = () => h("a", { class: "brand", href: "#/send" }, h("img", { src: "favicon.svg", alt: "" }), "Via");

let renderId = 0;

async function render() {
  const id = ++renderId;
  if (!state.info) state.info = await api("GET", "/v1/info").catch(() => null);
  if (!token()) return viewLogin();
  if (!state.user) {
    try {
      state.user = await api("GET", "/v1/account");
    } catch (err) {
      if (!token()) return viewLogin();
      $app.replaceChildren(h("p", { class: "boot" }, err.message));
      return;
    }
  }
  if (id !== renderId) return;

  let name = location.hash.replace(/^#\/?/, "") || "send";
  if (!VIEWS[name] || (name === "admin" && !state.user.is_admin)) name = "send";
  const nav = state.user.is_admin ? [...NAV, ["admin", "Admin"]] : NAV;
  const main = h("main", { class: "main" });
  $app.replaceChildren(
    h(
      "header",
      { class: "topbar" },
      brand(),
      h(
        "div",
        { class: "who" },
        h("span", { class: "muted" }, state.user.username),
        h("button", { class: "btn ghost small", onclick: signOut }, "Sign out"),
      ),
    ),
    h(
      "div",
      { class: "layout" },
      h(
        "nav",
        { class: "nav", "aria-label": "Main" },
        nav.map(([key, label]) =>
          h("a", { href: `#/${key}`, class: key === name ? "active" : null, "aria-current": key === name ? "page" : null }, label),
        ),
      ),
      main,
    ),
  );
  try {
    await VIEWS[name](main);
  } catch (err) {
    main.replaceChildren(card(h("p", { class: "form-error" }, err.message)));
  }
}

window.addEventListener("hashchange", render);
render();

// ---- sign in -----------------------------------------------------------------------------

function viewLogin() {
  const signup = state.info?.signup ?? "closed";
  let mode = "login";
  const box = h("div", { class: "auth-card" });

  const draw = () => {
    const register = mode === "register";
    const error = h("p", { class: "form-error", role: "alert" });
    const username = h("input", { autocomplete: "username", required: true, autofocus: true, maxlength: 64 });
    const password = h("input", {
      type: "password",
      required: true,
      autocomplete: register ? "new-password" : "current-password",
      minlength: register ? 8 : null,
    });
    const invite = register && signup === "invite" ? h("input", { required: true, placeholder: "via_i_…" }) : null;
    const submit = h("button", { class: "btn primary wide", type: "submit" }, register ? "Create account" : "Sign in");

    const form = h(
      "form",
      {
        onsubmit: async (e) => {
          e.preventDefault();
          error.textContent = "";
          busy(submit, true);
          try {
            const body = { username: username.value.trim(), password: password.value };
            if (invite) body.invite = invite.value.trim();
            const res = await api("POST", register ? "/v1/auth/register" : "/v1/auth/login", body);
            saveSession(res.token);
            state.user = null;
            location.hash = "#/send";
            render();
          } catch (err) {
            error.textContent = err.message;
            busy(submit, false);
          }
        },
      },
      field("Username", username, register ? "3–32 letters, digits, '_', '.' or '-'" : null),
      field("Password", password, register ? "At least 8 characters" : null),
      invite && field("Invite code", invite, "Ask the server's admin for one"),
      error,
      submit,
    );

    const toggle =
      signup === "closed"
        ? null
        : h(
            "p",
            { class: "muted center" },
            register ? "Already have an account? " : "No account yet? ",
            h(
              "a",
              {
                href: "#",
                onclick: (e) => {
                  e.preventDefault();
                  mode = register ? "login" : "register";
                  draw();
                },
              },
              register ? "Sign in" : "Create one",
            ),
          );

    box.replaceChildren(brand(), card(h("h1", {}, register ? "Create an account" : "Sign in"), form), toggle);
    username.focus();
  };

  $app.replaceChildren(h("div", { class: "auth" }, box));
  draw();
}

// ---- send --------------------------------------------------------------------------------

async function viewSend(main) {
  const [devices, contacts] = await Promise.all([api("GET", "/v1/devices"), api("GET", "/v1/contacts")]);
  const friends = contacts.filter((c) => c.status === "accepted");
  main.replaceChildren(pageHead("Send", "Send a link, a note or a file to your devices or to a contact."));

  if (!devices.length && !friends.length) {
    main.append(
      card(
        empty("You have nothing to send to yet."),
        h("div", {}, h("a", { class: "btn primary", href: "#/devices" }, "Register a device")),
      ),
    );
    return;
  }

  const limits = state.info?.limits;
  let kind = "link";
  let file = null;

  // Recipients
  const allBox = h("input", { type: "checkbox", checked: devices.length > 0 });
  const deviceBoxes = devices.map((d) => [d, h("input", { type: "checkbox", value: d.id })]);
  const contactBoxes = friends.map((c) => [c, h("input", { type: "checkbox", value: `@${c.username}` })]);
  const deviceChecks = h("div", { class: "targets" });
  const drawDeviceChecks = () =>
    deviceChecks.replaceChildren(
      ...deviceBoxes.map(([d, box]) => {
        box.disabled = allBox.checked;
        return check(`${d.name}`, box, allBox.checked);
      }),
    );
  allBox.addEventListener("change", drawDeviceChecks);
  drawDeviceChecks();

  const recipients = h(
    "div",
    { class: "targets-group" },
    devices.length ? check("All my devices", allBox) : null,
    devices.length ? deviceChecks : null,
    contactBoxes.length
      ? h("div", { class: "targets" }, contactBoxes.map(([c, box]) => check(`@${c.username}`, box)))
      : null,
  );

  // Content fields
  const url = h("input", { type: "url", placeholder: "https://…", maxlength: 8192 });
  const title = h("input", { maxlength: 1024, placeholder: "Optional" });
  const body = h("textarea", { maxlength: limits?.max_text_length ?? null });
  const fileInput = h("input", { type: "file" });
  const fileLabel = h("span", {}, h("strong", {}, "Choose a file"), " or drop it here");
  const drop = h("label", { class: "drop" }, fileInput, fileLabel, limits && h("div", { class: "hint" }, `Up to ${bytes(limits.max_file_size)}`));
  const setFile = (f) => {
    file = f;
    fileLabel.replaceChildren(f ? h("strong", {}, f.name) : h("strong", {}, "Choose a file"), f ? ` · ${bytes(f.size)}` : " or drop it here");
  };
  fileInput.addEventListener("change", () => setFile(fileInput.files[0] || null));
  drop.addEventListener("dragover", (e) => {
    e.preventDefault();
    drop.classList.add("over");
  });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("over");
    if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]);
  });

  const ttl = h(
    "select",
    {},
    h("option", { value: "" }, `Server default${limits ? ` (${duration(limits.default_ttl)})` : ""}`),
    [
      ["1h", 3600, "1 hour"],
      ["1d", 86400, "1 day"],
      ["7d", 604800, "7 days"],
      ["30d", 2592000, "30 days"],
    ]
      .filter(([, seconds]) => !limits || seconds <= limits.max_ttl)
      .map(([value, , label]) => h("option", { value }, label)),
  );

  const fields = h("div", { class: "targets-group" });
  const drawFields = () => {
    if (kind === "link") fields.replaceChildren(field("URL", url), field("Title", title));
    if (kind === "note") fields.replaceChildren(field("Title", title), field("Note", body));
    if (kind === "file") fields.replaceChildren(drop, field("Title", title), field("Message", body));
  };

  const tabs = h("div", { class: "segmented", role: "group", "aria-label": "Kind" });
  const drawTabs = () =>
    tabs.replaceChildren(
      ...[
        ["link", "Link"],
        ["note", "Note"],
        ["file", "File"],
      ].map(([value, label]) =>
        h(
          "button",
          {
            type: "button",
            "aria-pressed": String(kind === value),
            onclick: () => {
              kind = value;
              drawTabs();
              drawFields();
            },
          },
          label,
        ),
      ),
    );
  drawTabs();
  drawFields();

  const error = h("p", { class: "form-error", role: "alert" });
  const progress = h("div", { class: "progress", hidden: true }, h("div"));
  const submit = h("button", { class: "btn primary", type: "submit" }, "Send");

  const targets = () => {
    const picked = contactBoxes.filter(([, b]) => b.checked).map(([, b]) => b.value);
    if (allBox.checked) return picked.length ? [...devices.map((d) => d.id), ...picked] : "all";
    return [...deviceBoxes.filter(([, b]) => b.checked).map(([, b]) => b.value), ...picked];
  };

  const form = h(
    "form",
    {
      onsubmit: async (e) => {
        e.preventDefault();
        error.textContent = "";
        const to = targets();
        if (Array.isArray(to) && !to.length) {
          error.textContent = "Pick at least one recipient.";
          return;
        }
        const common = { title: title.value.trim() || null, ttl: ttl.value || null };
        busy(submit, true);
        try {
          let status;
          if (kind === "file") {
            if (!file) throw new ApiError("Choose a file first.");
            const params = new URLSearchParams({ filename: file.name, to: to === "all" ? "all" : to.join(",") });
            if (common.title) params.set("title", common.title);
            if (body.value.trim()) params.set("body", body.value.trim());
            if (common.ttl) params.set("ttl", common.ttl);
            progress.hidden = false;
            status = await upload(`/v1/pushes/file?${params}`, file, (p) => (progress.firstChild.style.width = `${Math.round(p * 100)}%`));
          } else {
            const payload = { kind, to, ...common };
            if (kind === "link") payload.url = url.value.trim();
            else payload.body = body.value;
            status = await api("POST", "/v1/pushes", payload);
          }
          const n = status.deliveries.length;
          toast(`Sent to ${n} device${n === 1 ? "" : "s"}`);
          url.value = "";
          title.value = "";
          body.value = "";
          fileInput.value = "";
          setFile(null);
        } catch (err) {
          error.textContent = err.message;
        } finally {
          busy(submit, false);
          progress.hidden = true;
          progress.firstChild.style.width = "0";
        }
      },
    },
    tabs,
    fields,
    field("To", recipients),
    h("div", { class: "form-row" }, field("Keep for", ttl, "Undelivered items are deleted after this")),
    error,
    progress,
    h("div", {}, submit),
  );
  main.append(card(form));
}

// ---- sent --------------------------------------------------------------------------------

const STATE_LABEL = { pending: "Waiting", delivered: "Seen", acked: "Received", expired: "Expired" };

async function viewSent(main) {
  const devices = await api("GET", "/v1/devices");
  const names = Object.fromEntries(devices.map((d) => [d.id, d.name]));
  const list = h("div", { class: "list" });
  const more = h("button", { class: "btn", type: "button", hidden: true }, "Load more");
  let before = null;

  const recipientName = (d) => (d.recipient ? `@${d.recipient}` : names[d.device_id] ?? "Removed device");

  const row = (push) => {
    const open = push.deliveries.some((d) => d.state === "pending" || d.state === "delivered");
    const recall = h("button", { class: "btn small danger", type: "button" }, "Recall");
    recall.addEventListener("click", () => {
      if (!confirm("Recall this item? Devices that haven't received it won't get it.")) return;
      act(recall, async () => {
        await api("DELETE", `/v1/pushes/${push.id}`);
        toast("Recalled");
        const fresh = await api("GET", `/v1/pushes/${push.id}`);
        item.replaceWith(row(fresh));
      });
    });
    const source = push.source_device_id ? names[push.source_device_id] ?? "a removed device" : "web or app token";
    const item = h(
      "div",
      { class: "row" },
      h(
        "div",
        { class: "row-main" },
        h("div", { class: "row-title" }, h("span", { class: "kind" }, push.kind), time(push.created_at), h("span", { class: "muted small" }, `from ${source}`)),
        h(
          "div",
          { class: "deliveries" },
          push.deliveries.map((d) =>
            h("span", { class: "delivery" }, recipientName(d), h("span", { class: `pill ${d.state}` }, STATE_LABEL[d.state] ?? d.state)),
          ),
        ),
        h(
          "span",
          { class: "muted small" },
          push.purged_at ? ["Content deleted ", time(push.purged_at)] : ["Expires ", time(push.expires_at)],
        ),
      ),
      h("div", { class: "row-actions" }, open && !push.purged_at ? recall : null),
    );
    return item;
  };

  const load = async () => {
    const params = new URLSearchParams({ limit: "25" });
    if (before) params.set("before", before);
    const page = await api("GET", `/v1/pushes/sent?${params}`);
    if (!page.length && !before) list.replaceChildren(empty("Nothing sent yet."));
    else list.append(...page.map(row));
    before = page.length ? page[page.length - 1].id : before;
    more.hidden = page.length < 25;
  };
  more.addEventListener("click", () => act(more, load));

  main.replaceChildren(
    pageHead("Sent", "Delivery status of what you sent. Only the receiving devices can read the content."),
    card(list, h("div", {}, more)),
  );
  await load();
}

// ---- devices -----------------------------------------------------------------------------

async function viewDevices(main) {
  const list = h("div", { class: "list" });
  const created = h("div");

  const refresh = async () => {
    const devices = await api("GET", "/v1/devices");
    list.replaceChildren(...(devices.length ? devices.map(deviceRow) : [empty("No devices yet. Register one below.")]));
  };

  const deviceRow = (d) => {
    const shares = h("input", { type: "checkbox", checked: d.accepts_shares });
    shares.addEventListener("change", async () => {
      try {
        await api("PATCH", `/v1/devices/${d.id}`, { accepts_shares: shares.checked });
        toast(shares.checked ? `${d.name} now receives shares` : `${d.name} no longer receives shares`);
      } catch (err) {
        shares.checked = !shares.checked;
        toast(err.message, "error");
      }
    });
    const rename = h("button", { class: "btn small", type: "button" }, "Rename");
    const remove = h("button", { class: "btn small danger", type: "button" }, "Remove");
    const title = h("div", { class: "row-title" }, d.name, h("span", { class: "kind" }, d.type));

    rename.addEventListener("click", () => {
      const input = h("input", { value: d.name, maxlength: 64, "aria-label": "Device name" });
      const save = h("button", { class: "btn small primary", type: "submit" }, "Save");
      const form = h(
        "form",
        {
          class: "form-row",
          onsubmit: (e) => {
            e.preventDefault();
            act(save, async () => {
              await api("PATCH", `/v1/devices/${d.id}`, { name: input.value.trim() });
              await refresh();
            });
          },
        },
        input,
        save,
        h("button", { class: "btn small ghost", type: "button", onclick: refresh }, "Cancel"),
      );
      title.replaceWith(form);
      input.focus();
    });
    remove.addEventListener("click", () => {
      if (!confirm(`Remove ${d.name}? Its token stops working and items waiting for it are dropped.`)) return;
      act(remove, async () => {
        await api("DELETE", `/v1/devices/${d.id}`);
        toast(`Removed ${d.name}`);
        await refresh();
      });
    });

    const push = { none: "No push", fcm_relay: "FCM", unifiedpush: "UnifiedPush" }[d.push_provider] ?? d.push_provider;
    return h(
      "div",
      { class: "row" },
      h(
        "div",
        { class: "row-main" },
        title,
        h("span", { class: "muted small" }, "Last seen ", time(d.last_seen_at), ` · ${push} · added `, time(d.created_at)),
        check("Receive items shared by contacts", shares),
      ),
      h("div", { class: "row-actions" }, rename, remove),
    );
  };

  // Register a device
  const name = h("input", { required: true, maxlength: 64, placeholder: "Pixel 9" });
  const type = h("select", {}, DEVICE_TYPES.map((t) => h("option", { value: t }, t)));
  const submit = h("button", { class: "btn primary", type: "submit" }, "Register");
  const form = h(
    "form",
    {
      onsubmit: (e) => {
        e.preventDefault();
        act(submit, async () => {
          const res = await api("POST", "/v1/devices", { name: name.value.trim(), type: type.value });
          name.value = "";
          created.replaceChildren(
            secretPanel(
              `Token for ${res.device.name}`,
              res.token,
              h("p", { class: "hint" }, "Enter it in the app together with the server address ", h("code", {}, location.origin), "."),
            ),
          );
          await refresh();
        });
      },
    },
    h("div", { class: "form-row" }, field("Name", name), field("Type", type), h("div", {}, submit)),
    h("p", { class: "hint" }, `Phones and desktops receive items shared by contacts by default (${SHARE_BY_DEFAULT.join(", ")}).`),
  );

  main.replaceChildren(
    pageHead("Devices", "Each device has its own token and can only read its own inbox."),
    card(list),
    card(h("h2", {}, "Register a device"), form, created),
  );
  await refresh();
}

// ---- contacts ----------------------------------------------------------------------------

async function viewContacts(main) {
  const lists = h("div", { class: "targets-group" });

  const refresh = async () => {
    const contacts = await api("GET", "/v1/contacts");
    const incoming = contacts.filter((c) => c.status === "pending" && c.direction === "incoming");
    const outgoing = contacts.filter((c) => c.status === "pending" && c.direction === "outgoing");
    const accepted = contacts.filter((c) => c.status === "accepted");

    const button = (label, cls, fn) => {
      const b = h("button", { class: `btn small ${cls}`, type: "button" }, label);
      b.addEventListener("click", () => act(b, async () => (await fn(), refresh())));
      return b;
    };
    const remove = (c, label, question) =>
      button(label, "danger", async () => {
        if (question && !confirm(question)) return;
        await api("DELETE", `/v1/contacts/${c.id}`);
      });
    const row = (c, meta, ...actions) =>
      h("div", { class: "row" }, h("div", { class: "row-main" }, h("div", { class: "row-title" }, `@${c.username}`), h("span", { class: "muted small" }, meta)), h("div", { class: "row-actions" }, actions));

    lists.replaceChildren(
      incoming.length
        ? card(
            h("h2", {}, "Requests"),
            h(
              "div",
              { class: "list" },
              incoming.map((c) =>
                row(c, ["Asked ", time(c.created_at)], button("Accept", "primary", () => api("POST", `/v1/contacts/${c.id}/accept`)), remove(c, "Decline")),
              ),
            ),
          )
        : null,
      card(
        h("h2", {}, "Contacts"),
        accepted.length
          ? h(
              "div",
              { class: "list" },
              accepted.map((c) => row(c, ["Contact since ", time(c.accepted_at)], remove(c, "Remove", `Remove @${c.username}? Neither of you will be able to send to the other.`))),
            )
          : empty("No contacts yet."),
      ),
      outgoing.length
        ? card(
            h("h2", {}, "Sent requests"),
            h("div", { class: "list" }, outgoing.map((c) => row(c, ["Sent ", time(c.created_at)], remove(c, "Cancel")))),
          )
        : null,
    );
  };

  const username = h("input", { required: true, maxlength: 64, placeholder: "username" });
  const submit = h("button", { class: "btn primary", type: "submit" }, "Send request");
  const form = h(
    "form",
    {
      onsubmit: (e) => {
        e.preventDefault();
        act(submit, async () => {
          const c = await api("POST", "/v1/contacts", { username: username.value.trim().replace(/^@/, "") });
          toast(c.status === "accepted" ? `You and @${c.username} are now contacts` : `Request sent to @${c.username}`);
          username.value = "";
          await refresh();
        });
      },
    },
    h("div", { class: "form-row" }, field("Add a contact", username), h("div", {}, submit)),
  );

  main.replaceChildren(
    pageHead("Contacts", "Contacts can send items to each other. Items go to the devices each person chose to receive shares on."),
    card(form),
    lists,
  );
  await refresh();
}

// ---- app tokens --------------------------------------------------------------------------

async function viewTokens(main) {
  const list = h("div", { class: "list" });
  const created = h("div");

  const refresh = async () => {
    const tokens = await api("GET", "/v1/app-tokens");
    list.replaceChildren(
      ...(tokens.length
        ? tokens.map((t) => {
            const remove = h("button", { class: "btn small danger", type: "button" }, "Delete");
            remove.addEventListener("click", () => {
              if (!confirm(`Delete "${t.name}"? Anything using it will stop working.`)) return;
              act(remove, async () => {
                await api("DELETE", `/v1/app-tokens/${t.id}`);
                await refresh();
              });
            });
            return h(
              "div",
              { class: "row" },
              h("div", { class: "row-main" }, h("div", { class: "row-title" }, t.name), h("span", { class: "muted small" }, "Last used ", time(t.last_used_at), " · created ", time(t.created_at))),
              h("div", { class: "row-actions" }, remove),
            );
          })
        : [empty("No app tokens.")]),
    );
  };

  const name = h("input", { required: true, maxlength: 64, placeholder: "Home server backups" });
  const submit = h("button", { class: "btn primary", type: "submit" }, "Create");
  const form = h(
    "form",
    {
      onsubmit: (e) => {
        e.preventDefault();
        act(submit, async () => {
          const res = await api("POST", "/v1/app-tokens", { name: name.value.trim() });
          name.value = "";
          const example = `curl -H "Authorization: Bearer ${res.token}" -d "Hello from a script" ${location.origin}/v1/send`;
          created.replaceChildren(
            secretPanel(`Token "${res.app_token.name}"`, res.token, h("p", { class: "hint" }, "Try it:"), h("div", { class: "secret-row" }, h("code", {}, example), copyButton(example))),
          );
          await refresh();
        });
      },
    },
    h("div", { class: "form-row" }, field("Name", name), h("div", {}, submit)),
  );

  main.replaceChildren(
    pageHead("App tokens", "Send-only tokens for scripts and integrations. They can't read anything."),
    card(list),
    card(h("h2", {}, "Create a token"), form, created),
  );
  await refresh();
}

// ---- account -----------------------------------------------------------------------------

async function viewAccount(main) {
  const account = await api("GET", "/v1/account");
  state.user = account;
  const { pending_bytes: used, quota } = account.usage;

  const current = h("input", { type: "password", required: true, autocomplete: "current-password" });
  const next = h("input", { type: "password", required: true, minlength: 8, autocomplete: "new-password" });
  const submit = h("button", { class: "btn primary", type: "submit" }, "Change password");
  const form = h(
    "form",
    {
      onsubmit: (e) => {
        e.preventDefault();
        act(submit, async () => {
          await api("PATCH", "/v1/account/password", { current_password: current.value, new_password: next.value });
          current.value = "";
          next.value = "";
          toast("Password changed. Other sessions were signed out.");
        });
      },
    },
    h("div", { class: "form-row" }, field("Current password", current), field("New password", next, "At least 8 characters")),
    h("div", {}, submit),
  );

  main.replaceChildren(
    pageHead("Account"),
    card(
      h("div", { class: "card-head" }, h("h2", {}, `@${account.username}`), account.is_admin ? h("span", { class: "pill ok" }, "Admin") : null),
      h("span", { class: "muted small" }, "Member since ", time(account.created_at)),
      h("div", { class: "field" }, h("span", { class: "field-label" }, `Storage: ${bytes(used)} of ${bytes(quota)}`), h("div", { class: "meter" }, h("div"))),
      h("span", { class: "hint" }, "Files count until every recipient has received them."),
    ),
    card(h("h2", {}, "Password"), form),
  );
  // Width is set from script: the CSP forbids inline style attributes.
  main.querySelector(".meter > div").style.width = `${Math.min(100, (used / quota) * 100)}%`;
}

// ---- admin -------------------------------------------------------------------------------

async function viewAdmin(main) {
  const statsBox = h("div", { class: "stats" });
  const users = h("div", { class: "table-wrap" });
  const invites = h("div", { class: "list" });
  const createdInvite = h("div");

  const refreshStats = async () => {
    const s = await api("GET", "/v1/admin/stats");
    statsBox.replaceChildren(
      ...[
        ["Users", s.users],
        ["Devices", s.devices],
        ["Stored items", s.pushes],
        ["Waiting deliveries", s.pending_deliveries],
        ["Stored files", bytes(s.stored_bytes)],
      ].map(([label, value]) => h("div", { class: "stat" }, h("span", { class: "muted small" }, label), h("b", {}, value))),
    );
  };

  const refreshUsers = async () => {
    const list = await api("GET", "/v1/admin/users");
    const me = state.user.id;
    const action = (label, cls, fn) => {
      const b = h("button", { class: `btn small ${cls}`, type: "button" }, label);
      b.addEventListener("click", () => act(b, async () => (await fn()) !== false && (await Promise.all([refreshUsers(), refreshStats()]))));
      return b;
    };
    users.replaceChildren(
      h(
        "table",
        {},
        h("thead", {}, h("tr", {}, h("th", {}, "User"), h("th", { class: "num" }, "Devices"), h("th", { class: "num" }, "Stored"), h("th", {}, "Last seen"), h("th", {}, "Created"), h("th", {}, ""))),
        h(
          "tbody",
          {},
          list.map((u) =>
            h(
              "tr",
              {},
              h("td", {}, h("span", { class: "row-title" }, u.username, u.is_admin ? h("span", { class: "pill ok" }, "Admin") : null, u.disabled_at ? h("span", { class: "pill bad" }, "Disabled") : null)),
              h("td", { class: "num" }, u.devices),
              h("td", { class: "num" }, bytes(u.stored_bytes)),
              h("td", {}, time(u.last_seen_at)),
              h("td", {}, time(u.created_at)),
              h(
                "td",
                {},
                u.id === me
                  ? h("span", { class: "muted small" }, "You")
                  : h(
                      "div",
                      { class: "row-actions" },
                      action(u.is_admin ? "Remove admin" : "Make admin", "", () => api("PATCH", `/v1/admin/users/${u.id}`, { is_admin: !u.is_admin })),
                      action(u.disabled_at ? "Enable" : "Disable", "", () => api("PATCH", `/v1/admin/users/${u.id}`, { disabled: !u.disabled_at })),
                      action("Reset password", "", async () => {
                        const password = prompt(`New password for ${u.username} (at least 8 characters):`);
                        if (!password) return false;
                        await api("POST", `/v1/admin/users/${u.id}/password`, { password });
                        toast(`Password of ${u.username} changed`);
                      }),
                      action("Delete", "danger", async () => {
                        if (!confirm(`Delete ${u.username} and all their devices, contacts, items and files? This can't be undone.`)) return false;
                        await api("DELETE", `/v1/admin/users/${u.id}`);
                        toast(`Deleted ${u.username}`);
                      }),
                    ),
              ),
            ),
          ),
        ),
      ),
    );
  };

  const refreshInvites = async () => {
    const list = await api("GET", "/v1/admin/invites");
    invites.replaceChildren(
      ...(list.length
        ? list.map((i) => {
            const remove = h("button", { class: "btn small danger", type: "button" }, "Revoke");
            remove.addEventListener("click", () => act(remove, async () => (await api("DELETE", `/v1/admin/invites/${i.id}`), refreshInvites())));
            return h("div", { class: "row" }, h("div", { class: "row-main" }, h("span", {}, "Unused invite, created ", time(i.created_at), ", expires ", time(i.expires_at))), h("div", { class: "row-actions" }, remove));
          })
        : [empty("No open invites.")]),
    );
  };

  // Create user
  const username = h("input", { required: true, minlength: 3, maxlength: 32 });
  const password = h("input", { type: "password", required: true, minlength: 8, autocomplete: "new-password" });
  const isAdmin = h("input", { type: "checkbox" });
  const createUser = h("button", { class: "btn primary", type: "submit" }, "Create user");
  const userForm = h(
    "form",
    {
      onsubmit: (e) => {
        e.preventDefault();
        act(createUser, async () => {
          const u = await api("POST", "/v1/admin/users", { username: username.value.trim(), password: password.value, is_admin: isAdmin.checked });
          toast(`Created ${u.username}`);
          username.value = "";
          password.value = "";
          isAdmin.checked = false;
          await Promise.all([refreshUsers(), refreshStats()]);
        });
      },
    },
    h("div", { class: "form-row" }, field("Username", username), field("Password", password)),
    check("Administrator", isAdmin),
    h("div", {}, createUser),
  );

  // Create invite
  const expires = h("select", {}, [["1d", "1 day"], ["7d", "7 days"], ["30d", "30 days"]].map(([v, l]) => h("option", { value: v, selected: v === "7d" }, l)));
  const createInvite = h("button", { class: "btn", type: "submit" }, "Create invite");
  const inviteForm = h(
    "form",
    {
      onsubmit: (e) => {
        e.preventDefault();
        act(createInvite, async () => {
          const inv = await api("POST", "/v1/admin/invites", { expires_in: expires.value });
          createdInvite.replaceChildren(secretPanel("Invite code", inv.code, h("p", { class: "hint" }, "Single use. Share it with the person you're inviting.")));
          await refreshInvites();
        });
      },
    },
    h("div", { class: "form-row" }, field("Expires after", expires), h("div", {}, createInvite)),
  );

  const signup = state.info?.signup ?? "closed";
  main.replaceChildren(
    pageHead("Admin"),
    card(statsBox),
    card(h("h2", {}, "Users"), users),
    card(h("h2", {}, "New user"), userForm),
    card(
      h("div", { class: "card-head" }, h("h2", {}, "Invites"), h("span", { class: "muted small" }, `Signup is ${signup}`)),
      invites,
      inviteForm,
      createdInvite,
    ),
  );
  await Promise.all([refreshStats(), refreshUsers(), refreshInvites()]);
}
