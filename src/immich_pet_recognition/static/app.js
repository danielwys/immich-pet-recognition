const $ = (id) => document.getElementById(id);
let token = sessionStorage.getItem("pet-service-token") || "";
let pets = [],
  status = {},
  panel = "review",
  filter = "pending",
  petFilter = "",
  offset = 0;
let visibleItems = [],
  selected = new Set(),
  objectUrls = [],
  refreshing = false,
  busy = false;
let reviewRequest = 0,
  pageLimit = 48,
  reviewFingerprint = "";
const titles = {
  review: [
    "Review your pets.",
    "Select photos, assign them together, and improve the next suggestions.",
  ],
  pets: [
    "Your pets. Their progress.",
    "See how many examples each pet has and what comes next.",
  ],
  processing: [
    "Library processing.",
    "Follow the scan and retry photos that need attention.",
  ],
  setup: [
    "Setup & connection.",
    "Your library and recognition models, running on your own hardware.",
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
function progressCard(pet) {
  const count = pet.positive_count,
    required = status.min_references;
  const card = node("article", "learning-card");
  const heading = node("div", "learning-heading");
  heading.append(
    node("strong", "", pet.name),
    node("span", "", `${count} / ${required} examples`),
  );
  const progress = node("progress");
  progress.max = required;
  progress.value = Math.min(count, required);
  progress.setAttribute("aria-label", `${pet.name} examples`);
  card.append(
    heading,
    progress,
    node(
      "p",
      "",
      count < required
        ? `${required - count} more to clear the example gate. Suggestions start with the first example.`
        : "Example gate met. Check accuracy on separate photos before enabling automatic tagging.",
    ),
  );
  return card;
}
function renderLearning() {
  const grid = $("learning-progress");
  grid.replaceChildren();
  if (!pets.length) {
    const intro = node("div", "learning-empty");
    intro.append(
      node("strong", "", "Start by naming your pet."),
      node(
        "p",
        "",
        "Then select a few clear photos and assign them together. Those examples unlock suggestions.",
      ),
      button("Register a pet", "primary", () => switchPanel("pets")),
    );
    grid.append(intro);
  }
  pets.forEach((pet) => grid.append(progressCard(pet)));
  $("suggestion-state").textContent = status.suggestions_updating
    ? "Updating suggestions across your library. Visible matches use your latest examples."
    : pets.some((p) => p.positive_count > 0)
      ? "Suggestions use your example galleries. Assignments can teach the next batch."
      : "No examples yet. Confirmations from the old interface were not used for matching.";
  $("match-guide-text").textContent =
    `All three gates must pass: ${status.min_references} examples for that pet, similarity at least ${status.match_threshold.toFixed(2)}, and a lead of at least ${status.match_margin.toFixed(2)} over the next pet or a negative example. Automatic tagging is ${status.auto_tag ? "on" : "off — even strong suggestions wait for your approval"}.`;
  $("match-guide-summary").textContent =
    `Strong suggestion: ${status.min_references} examples · similarity ≥ ${status.match_threshold.toFixed(2)} · lead ≥ ${status.match_margin.toFixed(2)}. How it works`;
}
async function refresh() {
  if (refreshing || busy) return;
  refreshing = true;
  try {
    [status, pets] = await Promise.all([api("status"), api("pets")]);
    $("demo-banner").classList.toggle("hidden", !status.demo);
    $("logout").classList.toggle("hidden", !status.auth_required);
    $("error-banner").classList.toggle("hidden", !status.last_error);
    $("error-banner").textContent = status.last_error || "";
    $("mode-label").textContent = status.demo
      ? "Offline demo"
      : "Self-hosted companion";
    $("mode-description").textContent = status.demo
      ? "Illustrations and simulated vectors. No real photos."
      : "Your photos, processed on your own hardware.";
    $("stat-pending").textContent = (
      status.reviews.pending || 0
    ).toLocaleString();
    $("nav-count").textContent = (status.reviews.pending || 0).toLocaleString();
    $("stat-confirmed").textContent = (
      status.reviews.confirmed || 0
    ).toLocaleString();
    $("stat-queued").textContent = (
      (status.jobs.queued || 0) + (status.jobs.processing || 0)
    ).toLocaleString();
    $("stat-pets").textContent = pets.length;
    $("last-sync").textContent = status.last_sync
      ? "Last sync · " + new Date(status.last_sync).toLocaleString()
      : "Waiting for first sync";
    $("tagging-state").textContent = status.auto_tag
      ? "Automatic tagging on"
      : "Automatic tagging off · Review required";
    renderLearning();
    renderPetFilters();
    updateSelection();
    if (!$("login-dialog").open && !(panel === "review" && selected.size))
      await renderPanel();
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
function renderPetFilters() {
  const container = $("pet-filters");
  container.replaceChildren();
  [["", "All pets"], ...pets.map((p) => [p.id, p.name])].forEach(
    ([id, name]) => {
      const b = button(
        name,
        "pet-chip" + (petFilter === id ? " active" : ""),
        () => {
          petFilter = id;
          offset = 0;
          selected.clear();
          renderPetFilters();
          return renderReview();
        },
      );
      b.setAttribute("aria-pressed", String(petFilter === id));
      b.title = id
        ? `Suggested or assigned to ${name}`
        : "Every detected animal";
      container.append(b);
    },
  );
}
function chosenItems() {
  return visibleItems.filter((item) => selected.has(item.id));
}
function updateSelection() {
  const chosen = chosenItems();
  const allShownSelected =
    visibleItems.length > 0 && chosen.length === visibleItems.length;
  document
    .querySelector(".batch-bar")
    .classList.toggle("has-selection", chosen.length > 0);
  $("selection-count").textContent = `${chosen.length} selected`;
  $("assignment-label").textContent =
    chosen.length && chosen.every((item) => item.status === "confirmed")
      ? "Change selected to:"
      : "Assign selected:";
  $("clear-selection").disabled = busy || !chosen.length;
  $("clear-selection").classList.toggle("hidden", allShownSelected);
  $("select-page").textContent = allShownSelected
    ? "Unselect all"
    : "Select shown";
  $("select-page").setAttribute("aria-pressed", String(allShownSelected));
  $("select-page").disabled = busy || !visibleItems.length;
  $("select-strong").disabled =
    busy ||
    !visibleItems.some(
      (item) => item.recommendation?.confident && !item.published,
    );
  $("accept-suggestions").disabled =
    busy ||
    !chosen.length ||
    chosen.some((item) => item.published || !item.recommendation?.confident);
  $("teach-confirmed").disabled =
    busy ||
    !chosen.length ||
    chosen.some((item) => item.status !== "confirmed" || !item.pet_id);
  $("remove-tags").disabled =
    busy ||
    !chosen.length ||
    chosen.some((item) => item.status !== "confirmed");
  $("remove-tags").classList.toggle(
    "hidden",
    !visibleItems.some((item) => item.status === "confirmed"),
  );
  $("dismiss-selected").disabled =
    busy || !chosen.length || chosen.some((item) => item.published);
  const assignments = $("batch-pets");
  assignments.replaceChildren();
  pets.forEach((pet) =>
    assignments.append(
      button(
        pet.name,
        "assign-button",
        () => assign(chosenItems(), pet),
        busy ||
          !chosen.length ||
          chosen.some((item) => item.species !== pet.species),
      ),
    ),
  );
  if (!pets.length)
    assignments.append(
      button("Register pet", "secondary", () => switchPanel("pets")),
    );
  document.querySelectorAll(".review-card").forEach((card) => {
    const checked = selected.has(card.dataset.id);
    card.classList.toggle("is-selected", checked);
    const checkbox = card.querySelector("input[type=checkbox]");
    if (checkbox) {
      checkbox.checked = checked;
      checkbox.disabled = busy;
    }
    card.querySelectorAll("button").forEach((b) => (b.disabled = busy));
  });
}
async function submitBatch(items, endpoint = "review/batch") {
  if (busy || !items.length) return;
  busy = true;
  $("batch-feedback").textContent = `Saving ${items.length} photos…`;
  updateSelection();
  let result;
  try {
    result = await api(endpoint, {
      method: "POST",
      body: JSON.stringify({ items }),
    });
    result.results.forEach((item) => {
      if (item.saved) selected.delete(item.id);
    });
    const failures = result.results.filter((item) => !item.saved);
    $("batch-feedback").textContent =
      `${result.saved} saved${result.failed ? ` · ${result.failed} could not be saved: ${failures[0].error}` : ""}. ${endpoint === "references/batch" || items.some((item) => item.learn) ? "Examples updated. Suggestions are being refreshed." : ""}${status.demo ? " Demo only; no Immich tags written." : endpoint === "review/batch" ? " Immich tag changes queued." : ""}`;
  } catch (error) {
    $("batch-feedback").textContent = error.message;
  } finally {
    busy = false;
  }
  if (result) {
    // Keep failed rows visible so partial failures can be corrected without losing the selection.
    selected.clear();
    offset = 0;
    await refresh();
    const failedIds = new Set(
      result.results.filter((item) => !item.saved).map((item) => item.id),
    );
    visibleItems.forEach((item) => {
      if (failedIds.has(item.id)) selected.add(item.id);
    });
  }
  updateSelection();
}
function assign(items, pet) {
  const learn = $("learn-from-assignments").checked;
  return submitBatch(
    items.map((item) => ({
      detection_id: item.id,
      status: "confirmed",
      pet_id: pet.id,
      learn,
      expected_pet_id: item.status === "confirmed" ? item.pet_id : null,
    })),
  );
}
function dismiss(items) {
  return submitBatch(
    items.map((item) => ({
      detection_id: item.id,
      status: "rejected",
      pet_id: null,
      learn: false,
      expected_pet_id: item.status === "confirmed" ? item.pet_id : null,
    })),
  );
}
function teachConfirmed(items) {
  return submitBatch(
    items.map((item) => ({
      detection_id: item.id,
      pet_id: item.pet_id,
      kind: "positive",
    })),
    "references/batch",
  );
}
async function notThisPet(item) {
  if (!item.recommendation || busy) return;
  return submitBatch(
    [
      {
        detection_id: item.id,
        pet_id: item.recommendation.pet_id,
        kind: "negative",
      },
    ],
    "references/batch",
  );
}
function renderGates(recommendation) {
  const details = node("details", "match-reasons");
  details.append(
    node(
      "summary",
      "",
      `Why this suggestion? · similarity ${recommendation.score.toFixed(2)}`,
    ),
  );
  recommendation.gates.forEach((gate) => {
    const value =
      gate.label === "Examples"
        ? `${gate.value} / ${gate.required}`
        : `${gate.value.toFixed(2)} / ${gate.required.toFixed(2)} minimum`;
    details.append(
      node(
        "p",
        gate.passed ? "gate-pass" : "gate-miss",
        `${gate.passed ? "✓" : "○"} ${gate.label}: ${value}`,
      ),
    );
  });
  details.append(node("p", "", "Similarity is not a probability."));
  return details;
}
async function renderReview() {
  const request = ++reviewRequest;
  const query = new URLSearchParams({
    status: filter,
    offset,
    limit: pageLimit,
    sort: $("review-sort").value,
  });
  if (petFilter) query.set("pet_id", petFilter);
  const data = await api("detections?" + query);
  if (panel !== "review" || request !== reviewRequest) return;
  if (data.total && offset >= data.total) {
    offset = Math.floor((data.total - 1) / pageLimit) * pageLimit;
    return renderReview();
  }
  const fingerprint = query.toString() + JSON.stringify(data);
  if (fingerprint === reviewFingerprint) {
    updateSelection();
    return;
  }
  reviewFingerprint = fingerprint;
  objectUrls.forEach(URL.revokeObjectURL);
  objectUrls = [];
  visibleItems = data.items;
  const visibleIds = new Set(visibleItems.map((item) => item.id));
  selected = new Set([...selected].filter((id) => visibleIds.has(id)));
  const grid = $("review-grid");
  grid.replaceChildren();
  $("review-total").textContent = `${data.total.toLocaleString()} detections`;
  $("page-range").textContent = data.total
    ? `${offset + 1}–${Math.min(offset + data.limit, data.total)} of ${data.total.toLocaleString()}`
    : "No detections here";
  $("previous").disabled = offset === 0;
  $("next").disabled = offset + data.limit >= data.total;
  if (!visibleItems.length) {
    const empty = node("div", "empty");
    empty.append(
      node(
        "h2",
        "",
        petFilter ? "No matches for this pet yet." : "No photos waiting here.",
      ),
      node(
        "p",
        "",
        petFilter
          ? "Switch to All pets to choose examples. Suggestions appear after examples are added and matching catches up."
          : "New detections appear after the library scan. You can also turn existing confirmed photos into examples.",
      ),
      button(
        pets.length ? "Show all pets" : "Register your pet",
        "primary",
        () => {
          if (!pets.length) return switchPanel("pets");
          petFilter = "";
          renderPetFilters();
          return renderReview();
        },
      ),
    );
    grid.append(empty);
  }
  visibleItems.forEach((item) => {
    const rec = item.recommendation;
    const card = node("article", "review-card");
    card.dataset.id = item.id;
    const wrap = node("label", "crop-wrap");
    const checkbox = node("input", "crop-select");
    checkbox.type = "checkbox";
    checkbox.setAttribute("aria-label", "Select " + item.name);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selected.add(item.id);
      else selected.delete(item.id);
      updateSelection();
    });
    const img = node("img");
    img.alt = `Detected ${item.species} in ${item.name}`;
    img.loading = "lazy";
    wrap.append(img, node("span", "species", item.species), checkbox);
    card.append(wrap);
    const content = node("div", "card-content"),
      line = node("div", "card-line");
    line.append(
      node(
        "strong",
        "card-name",
        item.status === "confirmed"
          ? item.pet_name
          : rec
            ? rec.pet_name + "?"
            : "No identity suggestion",
      ),
    );
    if (item.status === "pending")
      line.append(
        node(
          "span",
          "quality " + (rec?.confident ? "strong" : ""),
          !rec
            ? pets.some(
                (pet) => pet.species === item.species && pet.positive_count > 0,
              )
              ? "Unmatched"
              : "Needs examples"
            : rec.confident
              ? "Strong suggestion"
              : rec.references < status.min_references
                ? "Early suggestion"
                : "Check identity",
        ),
      );
    else
      line.append(
        node(
          "span",
          "quality",
          item.status === "confirmed" ? "Confirmed" : "Dismissed",
        ),
      );
    content.append(line, node("p", "filename", item.name));
    if (rec) content.append(renderGates(rec));
    {
      const actions = node("div", "card-actions");
      if (rec) {
        actions.append(
          button("✓ " + rec.pet_name, "primary", () =>
            assign(
              [item],
              pets.find((p) => p.id === rec.pet_id),
            ),
          ),
        );
        actions.append(
          button("Not " + rec.pet_name, "secondary", () => notThisPet(item)),
        );
      }
      content.append(actions);
      const options = node("div", "pet-options");
      const compatible = pets.filter(
        (pet) => pet.species === item.species && pet.id !== rec?.pet_id,
      );
      if (compatible.length)
        options.append(
          node(
            "span",
            "",
            item.status === "confirmed" ? "Change to:" : "Assign:",
          ),
        );
      compatible.forEach((pet) =>
        options.append(
          button(pet.name, "assign-button", () => assign([item], pet)),
        ),
      );
      options.append(
        button(
          item.status === "confirmed" ? "Remove tag" : "Dismiss",
          "quiet",
          () => dismiss([item]),
        ),
      );
      content.append(options);
    }
    if (item.status === "confirmed") {
      content.append(
        node(
          "p",
          "review-state",
          item.published
            ? "✓ Tagged in Immich"
            : status.demo
              ? "Confirmed in demo"
              : "Tag writeback queued",
        ),
      );
      content.append(
        button("Use as example", "reference-action", () =>
          teachConfirmed([item]),
        ),
      );
    }
    if (item.error) content.append(node("p", "error-text", item.error));
    card.append(content);
    grid.append(card);
    loadCrop(item.id, img).catch(() => {});
  });
  updateSelection();
}
function renderPets() {
  const grid = $("pet-grid");
  grid.replaceChildren();
  pets.forEach((pet) => {
    const card = node("article", "pet-card");
    card.append(
      node("h3", "", pet.name),
      node("p", "", `${pet.species} · Immich tag: ${pet.tag}`),
      progressCard(pet),
    );
    card.append(
      node("p", "", `${pet.negative_count} look-alike examples excluded`),
    );
    card.append(
      button("Choose example photos →", "quiet", () => {
        petFilter = "";
        filter = "pending";
        offset = 0;
        selected.clear();
        updateFilterButtons();
        renderPetFilters();
        switchPanel("review");
      }),
    );
    card.append(
      button("Use existing confirmations →", "quiet", () => {
        petFilter = pet.id;
        filter = "confirmed";
        offset = 0;
        selected.clear();
        updateFilterButtons();
        renderPetFilters();
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
      status.auth_required
        ? "Service token required"
        : "Direct access · trusted network",
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
      ? "Demo only. No Immich connection."
      : "Library scan queued. Follow it in Processing.",
  );
}
function updateFilterButtons() {
  document.querySelectorAll("[data-filter]").forEach((b) => {
    b.classList.toggle("selected", b.dataset.filter === filter);
    b.setAttribute("aria-pressed", String(b.dataset.filter === filter));
  });
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
    selected.clear();
    updateFilterButtons();
    renderReview().catch((e) => toast(e.message));
  }),
);
$("review-sort").addEventListener("change", () => {
  offset = 0;
  selected.clear();
  renderReview().catch((e) => toast(e.message));
});
$("select-page").addEventListener("click", () => {
  if (
    visibleItems.length &&
    visibleItems.every((item) => selected.has(item.id))
  ) {
    selected.clear();
  } else {
    visibleItems.forEach((item) => selected.add(item.id));
  }
  updateSelection();
});
$("select-strong").addEventListener("click", () => {
  selected.clear();
  visibleItems
    .filter((item) => item.recommendation?.confident && !item.published)
    .forEach((item) => selected.add(item.id));
  updateSelection();
});
$("clear-selection").addEventListener("click", () => {
  selected.clear();
  updateSelection();
});
$("accept-suggestions").addEventListener("click", () =>
  submitBatch(
    chosenItems().map((item) => ({
      detection_id: item.id,
      status: "confirmed",
      pet_id: item.recommendation.pet_id,
      learn: $("learn-from-assignments").checked,
    })),
  ),
);
$("teach-confirmed").addEventListener("click", () =>
  teachConfirmed(chosenItems()),
);
$("remove-tags").addEventListener("click", () => dismiss(chosenItems()));
$("dismiss-selected").addEventListener("click", () => dismiss(chosenItems()));
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
    toast("Suggestions queued for refresh.");
  } catch (e) {
    toast(e.message);
  }
});
$("retry").addEventListener("click", async () => {
  try {
    await api("retry", { method: "POST" });
    toast("Failures queued for retry.");
  } catch (e) {
    toast(e.message);
  }
});
$("previous").addEventListener("click", () => {
  offset = Math.max(0, offset - pageLimit);
  selected.clear();
  renderReview().catch((e) => toast(e.message));
});
$("next").addEventListener("click", () => {
  offset += pageLimit;
  selected.clear();
  renderReview().catch((e) => toast(e.message));
});
$("pet-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("pets", {
      method: "POST",
      body: JSON.stringify({
        name: $("pet-name").value,
        species: $("pet-species").value,
      }),
    });
    $("pet-form").reset();
    toast("Pet registered. Select example photos to get started.");
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
  if (!$("login-dialog").open && !document.hidden && !busy)
    refresh().catch(() => {});
}, 10000);
