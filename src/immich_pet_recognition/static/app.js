const $ = (id) => document.getElementById(id);
let token = sessionStorage.getItem("pet-service-token") || "";
let pets = [],
  status = {},
  activePet = "",
  panel = "review",
  filter = "pending",
  offset = 0;
let objectUrls = [],
  refreshing = false;
const titles = {
  review: [
    "Every photo. Your pet.",
    "Review the animals we found, and tell us who belongs to your family.",
  ],
  pets: [
    "Meet your family.",
    "A few good reference photos are the beginning of a familiar face.",
  ],
  processing: [
    "A little work. A lot of photos.",
    "Follow your library scan, and pick up where you left off.",
  ],
  setup: [
    "Close to home.",
    "Your library and your recognition service, on your own hardware.",
  ],
};
function toast(message) {
  $("toast").textContent = message;
  $("toast").classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $("toast").classList.add("hidden"), 4500);
}
async function api(path, options = {}) {
  const response = await fetch("/api/" + path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: "Bearer " + token } : {}),
      ...options.headers,
    },
  });
  if (response.status === 401) {
    if (!$("login-dialog").open) $("login-dialog").showModal();
    throw new Error("Enter your service token to connect.");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : "Please check the form and try again.",
    );
  }
  return response.json();
}
function node(tag, className, text) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text !== undefined) n.textContent = text;
  return n;
}
function button(text, className, action, disabled = false) {
  const b = node("button", className, text);
  b.disabled = disabled;
  b.addEventListener("click", () =>
    Promise.resolve(action()).catch((e) => toast(e.message)),
  );
  return b;
}
function selectedPet() {
  return pets.find((p) => p.id === activePet);
}
function switchPanel(value) {
  panel = value;
  document
    .querySelectorAll(".panel")
    .forEach((p) => p.classList.toggle("hidden", p.id !== panel + "-panel"));
  document
    .querySelectorAll(".nav")
    .forEach((n) => n.classList.toggle("active", n.dataset.panel === panel));
  $("page-title").textContent = titles[panel][0];
  $("page-description").textContent = titles[panel][1];
  $("breadcrumb").textContent = panel.toUpperCase();
  renderPanel().catch((e) => toast(e.message));
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    [status, pets] = await Promise.all([api("status"), api("pets")]);
    if (!pets.some((p) => p.id === activePet)) activePet = pets[0]?.id || "";
    const picker = $("active-pet");
    picker.replaceChildren();
    if (!pets.length) picker.append(new Option("Register a pet first", ""));
    pets.forEach((p) => picker.append(new Option(p.name, p.id)));
    picker.value = activePet;
    $("demo-banner").classList.toggle("hidden", !status.demo);
    $("logout").classList.toggle("hidden", !status.auth_required);
    $("error-banner").classList.toggle("hidden", !status.last_error);
    $("error-banner").textContent = status.last_error || "";
    $("mode-label").textContent = status.demo
      ? "Offline demo"
      : "Self-hosted companion";
    $("mode-description").textContent = status.demo
      ? "Sample illustrations. No photos leave this demo."
      : "Your photos, processed on your own hardware.";
    $("stat-pending").textContent = status.reviews.pending || 0;
    $("nav-count").textContent = status.reviews.pending || 0;
    $("stat-confirmed").textContent = status.reviews.confirmed || 0;
    $("stat-queued").textContent =
      (status.jobs.queued || 0) + (status.jobs.processing || 0);
    $("stat-pets").textContent = pets.length;
    $("last-sync").textContent = status.last_sync
      ? "Last sync · " + new Date(status.last_sync).toLocaleString()
      : "Waiting for first sync";
    $("tagging-state").textContent = status.auto_tag
      ? "Automatic tagging enabled"
      : "Automatic tagging off · Human review comes first";
    if (!$("login-dialog").open) await renderPanel();
  } finally {
    refreshing = false;
  }
}
async function renderPanel() {
  if (panel === "review") await renderReview();
  if (panel === "pets") renderPets();
  if (panel === "processing") await renderJobs();
  if (panel === "setup") renderSetup();
}
async function loadCrop(id, image) {
  const response = await fetch("/api/detections/" + id + "/crop", {
    headers: token ? { Authorization: "Bearer " + token } : {},
  });
  if (!response.ok) return;
  const url = URL.createObjectURL(await response.blob());
  if (!image.isConnected) {
    URL.revokeObjectURL(url);
    return;
  }
  objectUrls.push(url);
  image.src = url;
}
async function decision(item, confirmed) {
  const pet = selectedPet();
  if (confirmed && !pet)
    return toast("Register a pet in the Pet gallery first.");
  if (!confirmed && pet && pet.species === item.species)
    await api("pets/" + pet.id + "/references", {
      method: "POST",
      body: JSON.stringify({ detection_id: item.id, kind: "negative" }),
    });
  await api("detections/" + item.id + "/decision", {
    method: "POST",
    body: JSON.stringify({
      status: confirmed ? "confirmed" : "rejected",
      pet_id: confirmed ? pet.id : null,
    }),
  });
  toast(
    confirmed
      ? status.demo
        ? "Confirmed in the demo. No Immich tag was written."
        : "Confirmed. Tag writeback is queued."
      : "Dismissed. Negative example saved when a pet was selected.",
  );
  await refresh();
}
async function renderReview() {
  const requestedFilter = filter,
    requestedOffset = offset;
  const data = await api("detections?status=" + filter + "&offset=" + offset);
  if (
    panel !== "review" ||
    requestedFilter !== filter ||
    requestedOffset !== offset
  )
    return;
  objectUrls.forEach(URL.revokeObjectURL);
  objectUrls = [];
  const grid = $("review-grid");
  grid.replaceChildren();
  $("review-total").textContent = data.total + " detections";
  $("page-range").textContent = data.total
    ? `${offset + 1}–${Math.min(offset + data.limit, data.total)} of ${data.total}`
    : "No detections yet";
  $("previous").disabled = offset === 0;
  $("next").disabled = offset + data.limit >= data.total;
  if (!data.items.length) {
    const empty = node("div", "empty");
    empty.append(
      node("h2", "", "All caught up."),
      node(
        "p",
        "",
        pets.length
          ? "Sync your library to find more animals, or switch filters to see your previous decisions."
          : "Register your pet, then scan your library. Add clear reference crops before relying on identity matches.",
      ),
    );
    empty.append(
      button(
        pets.length ? "Sync library" : "Register your pet",
        "primary",
        () => (pets.length ? sync(false) : switchPanel("pets")),
      ),
    );
    grid.append(empty);
  }
  data.items.forEach((item) => {
    const pet = selectedPet(),
      matchesSpecies = pet && pet.species === item.species;
    const card = node("article", "review-card");
    const wrap = node("div", "crop-wrap");
    const img = node("img");
    img.alt = "Detected " + item.species + " in " + item.name;
    wrap.append(img, node("span", "species", item.species));
    card.append(wrap);
    const content = node("div", "card-content"),
      line = node("div", "card-line");
    line.append(
      node("span", "card-name", item.pet_name || "Who is this?"),
      node("span", "similarity", "Similarity " + item.score.toFixed(2)),
    );
    content.append(line, node("p", "filename", item.name));
    if (item.published)
      content.append(node("p", "review-state", "✓ Tagged in Immich"));
    else {
      const actions = node("div", "card-actions");
      actions.append(
        button(
          "Confirm " + (pet?.name || "pet"),
          "primary",
          () => decision(item, true),
          !matchesSpecies,
        ),
      );
      actions.append(
        button("Dismiss", "secondary", () => decision(item, false)),
      );
      content.append(actions);
      content.append(
        button(
          "＋ Add reference for " + (pet?.name || "your pet"),
          "reference-action",
          async () => {
            await api("pets/" + pet.id + "/references", {
              method: "POST",
              body: JSON.stringify({ detection_id: item.id, kind: "positive" }),
            });
            toast("Reference saved. Cached crops will be rechecked.");
            await refresh();
          },
          !matchesSpecies,
        ),
      );
      if (item.status !== "pending")
        content.append(
          node(
            "p",
            "review-state",
            item.status === "confirmed"
              ? status.demo
                ? "Confirmed in demo"
                : "Confirmed · awaiting tag writeback"
              : "Dismissed",
          ),
        );
    }
    if (item.error) content.append(node("p", "error-text", item.error));
    card.append(content);
    grid.append(card);
    loadCrop(item.id, img).catch(() => {});
  });
}
function renderPets() {
  const grid = $("pet-grid");
  grid.replaceChildren();
  pets.forEach((p) => {
    const card = node("article", "pet-card");
    card.append(
      node("h3", "", p.name),
      node("p", "", p.species + " · Immich tag: " + p.tag),
    );
    const counts = node("div", "counts");
    counts.append(
      node("span", "", p.positive_count + " reference crops"),
      node("span", "", p.negative_count + " negative examples"),
    );
    card.append(counts);
    card.append(
      node(
        "p",
        "",
        p.positive_count < status.min_references
          ? "Add more varied references before enabling automatic tagging."
          : "Gallery ready for a separate accuracy check.",
      ),
    );
    card.append(
      button("Add references from review →", "quiet", () => {
        activePet = p.id;
        $("active-pet").value = activePet;
        switchPanel("review");
      }),
    );
    grid.append(card);
  });
}
async function renderJobs() {
  const jobs = await api("jobs"),
    list = $("job-list");
  list.replaceChildren();
  if (!jobs.length)
    list.append(
      node(
        "div",
        "job-row",
        status.demo
          ? "Demo mode. Real library processing is disabled."
          : "No jobs yet. Scan your existing library to get started.",
      ),
    );
  jobs.forEach((j) => {
    const row = node("div", "job-row"),
      title = node("div", "", j.name);
    if (j.error) title.append(node("small", "", j.error));
    row.append(
      title,
      node("span", "job-status", j.status + " · attempts " + j.attempts),
    );
    list.append(row);
  });
}
function renderSetup() {
  const grid = $("setup-info");
  grid.replaceChildren();
  [
    [
      "Mode",
      status.demo
        ? "Offline demo · simulated inference"
        : status.immich_connected
          ? "Immich connected"
          : "Awaiting a successful Immich sync",
    ],
    ["Automatic tagging", status.auto_tag ? "On" : "Off · review required"],
    [
      "Access",
      status.auth_required ? "Service token required" : "Direct access · trusted network",
    ],
    ["Detector", status.detector],
    ["Recognition model", status.recognizer],
    ["Execution provider", status.provider],
    [
      "Matching defaults",
      `Similarity ≥ ${status.match_threshold} · Margin ≥ ${status.match_margin} · ${status.min_references}+ references`,
    ],
  ].forEach(([label, value]) => {
    const card = node("div", "setup-card");
    card.append(node("span", "", label), node("strong", "", value));
    grid.append(card);
  });
}
async function sync(full) {
  await api("sync?full=" + full, { method: "POST" });
  toast(
    status.demo
      ? "Demo only. Connect a real Immich instance to scan photos."
      : "Library scan queued. Follow it in Processing.",
  );
}
document
  .querySelectorAll("[data-panel]")
  .forEach((b) =>
    b.addEventListener("click", () => switchPanel(b.dataset.panel)),
  );
document.querySelectorAll("[data-filter]").forEach((b) =>
  b.addEventListener("click", () => {
    filter = b.dataset.filter;
    offset = 0;
    document.querySelectorAll("[data-filter]").forEach((x) => {
      x.classList.toggle("selected", x === b);
      x.setAttribute("aria-pressed", String(x === b));
    });
    renderReview().catch((e) => toast(e.message));
  }),
);
$("active-pet").addEventListener("change", () => {
  activePet = $("active-pet").value;
  renderReview().catch((e) => toast(e.message));
});
$("refresh").addEventListener("click", () =>
  refresh().catch((e) => toast(e.message)),
);
$("sync").addEventListener("click", () =>
  sync(false).catch((e) => toast(e.message)),
);
$("backfill").addEventListener("click", () =>
  sync(true).catch((e) => toast(e.message)),
);
$("rematch").addEventListener("click", async () => {
  try {
    await api("rematch", { method: "POST" });
    toast("Cached crops queued for rechecking.");
  } catch (e) {
    toast(e.message);
  }
});
$("retry").addEventListener("click", async () => {
  try {
    await api("retry", { method: "POST" });
    toast("Failed jobs and tag writes queued for retry.");
  } catch (e) {
    toast(e.message);
  }
});
$("previous").addEventListener("click", () => {
  offset = Math.max(0, offset - 24);
  renderReview().catch((e) => toast(e.message));
});
$("next").addEventListener("click", () => {
  offset += 24;
  renderReview().catch((e) => toast(e.message));
});
$("pet-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const pet = await api("pets", {
      method: "POST",
      body: JSON.stringify({
        name: $("pet-name").value,
        species: $("pet-species").value,
      }),
    });
    activePet = pet.id;
    $("pet-form").reset();
    toast("Pet registered. Add reference crops from the review queue.");
    await refresh();
  } catch (error) {
    toast(error.message);
  }
});
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  token = $("service-token").value;
  try {
    await api("status");
    sessionStorage.setItem("pet-service-token", token);
    $("service-token").value = "";
    $("login-dialog").close();
    await refresh();
  } catch (error) {
    $("login-error").textContent = error.message;
  }
});
$("login-dialog").addEventListener("cancel", (e) => e.preventDefault());
$("logout").addEventListener("click", () => {
  token = "";
  sessionStorage.removeItem("pet-service-token");
  refresh().catch((e) => toast(e.message));
});
refresh().catch((e) => {
  if (!$("login-dialog").open) toast(e.message);
});
setInterval(() => {
  if (panel !== "pets" && !$("login-dialog").open && !document.hidden)
    refresh().catch(() => {});
}, 10000);
