const $ = (id) => document.getElementById(id);
let token = sessionStorage.getItem("pet-service-token") || "";
let pets = [],
  status = {},
  panel = "review",
  filter = "untagged",
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
    "Tag your pets.",
    "Start with untagged photos. Tagged keeps manual and automatic results together.",
  ],
  pets: [
    "Teach. Automate. Check.",
    "Each pet has its own automatic tagging control. Uncertain matches always wait for review.",
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
    node("span", "", `${count} examples · ${required} minimum`),
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
        ? `Add ${required - count} more examples to reach the minimum for automatic tagging.`
        : "Example minimum reached. Each new match must also pass the similarity and look-alike checks.",
    ),
  );
  const mode = node(
    "strong",
    "automation-mode" + (pet.auto_tag ? " is-on" : ""),
    pet.auto_tag
      ? "Automatic tagging ON"
      : "Review only · automatic tagging OFF",
  );
  card.append(mode);
  card.append(
    node(
      "p",
      "automation-summary",
      `${pet.auto_tagged_count.toLocaleString()} automatically tagged · ${pet.pending_count.toLocaleString()} to review`,
    ),
  );
  card.append(
    node(
      "p",
      "",
      status.suggestions_updating
        ? "Rechecking the queue with your latest examples and settings…"
        : `${pet.strong_count.toLocaleString()} waiting matches pass all three checks${pet.auto_tag ? "." : " · enable to tag them."}`,
    ),
  );
  if (pet.queued_count)
    card.append(
      node("p", "", `${pet.queued_count} tags waiting to sync to Immich.`),
    );
  const control = button(
    pet.auto_tag ? "Pause automatic tagging" : "Enable automatic tagging",
    pet.auto_tag ? "secondary" : "primary",
    () => setAutomation(pet),
    busy || (!pet.auto_tag && count < required),
  );
  control.setAttribute(
    "aria-label",
    `${pet.auto_tag ? "Pause" : "Enable"} automatic tagging for ${pet.name}`,
  );
  card.append(control);
  card.append(
    node(
      "p",
      "next-step",
      pet.auto_tag
        ? "Checks the existing queue and new photos. Pause stops future automatic tags; existing tags stay."
        : count < required
          ? "Next: assign clear photos to this pet with “Use my assignments as examples” checked."
          : `Next: enable to tag strong matches in the existing queue and new photos as ${pet.tag}.`,
    ),
  );
  return card;
}
function automationSummary() {
  const names = pets.filter((pet) => pet.auto_tag).map((pet) => pet.name);
  return names.length
    ? `Automatic tagging on for ${names.join(" and ")}`
    : "Automatic tagging off for all pets";
}
async function setAutomation(pet) {
  if (busy) return;
  busy = true;
  renderLearning();
  if (panel === "pets") renderPets();
  try {
    await api(`pets/${pet.id}/automation`, {
      method: "PUT",
      body: JSON.stringify({ enabled: !pet.auto_tag }),
    });
    toast(
      pet.auto_tag
        ? `${pet.name}: automatic tagging paused. Existing tags stay.`
        : `${pet.name}: automatic tagging enabled. Rechecking the existing queue.`,
    );
  } finally {
    busy = false;
    await refresh();
  }
}
function showPetReview(pet, nextFilter) {
  petFilter = pet?.id || "";
  filter = nextFilter === "pending" ? "untagged" : "tagged";
  $("tag-source").value = nextFilter === "automatic" ? "automatic" : "all";
  document.querySelector(".more-filters").open = nextFilter === "automatic";
  $("include-dismissed").checked = false;
  offset = 0;
  selected.clear();
  updateFilterButtons();
  renderPetFilters();
  switchPanel("review");
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
  $("review-automation-summary").textContent =
    automationSummary() + " · Manage";
  $("match-guide-text").textContent =
    `A match must pass all three checks: ${status.min_references} examples for that pet, similarity ≥ ${status.match_threshold.toFixed(2)}, and a lead ≥ ${status.match_margin.toFixed(2)} over look-alikes. Enable automatic tagging in Pets & automation. Human assignments teach the gallery; automatic tags never do.`;
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
    $("tagging-state").textContent = automationSummary();
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
        ? filter === "tagged"
          ? `Photos already tagged as ${name}`
          : `Suggested or assigned to ${name}`
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
  $("clear-selection").classList.toggle(
    "hidden",
    allShownSelected || !chosen.length,
  );
  document
    .querySelector(".batch-actions")
    .classList.toggle("hidden", !chosen.length);
  document
    .querySelector(".learn-option")
    .classList.toggle("hidden", !chosen.length);
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
  $("select-strong").classList.toggle(
    "hidden",
    !visibleItems.some((item) => item.recommendation?.confident),
  );
  $("accept-suggestions").classList.toggle(
    "hidden",
    !visibleItems.some((item) => item.recommendation),
  );
  $("accept-suggestions").disabled =
    busy ||
    !chosen.length ||
    chosen.some((item) => item.published || !item.recommendation?.confident);
  $("teach-confirmed").disabled =
    busy ||
    !chosen.length ||
    chosen.some((item) => item.status !== "confirmed" || !item.pet_id);
  $("teach-confirmed").classList.toggle(
    "hidden",
    !visibleItems.some((item) => item.status === "confirmed"),
  );
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
  $("dismiss-selected").classList.toggle(
    "hidden",
    !visibleItems.some((item) => item.status === "pending"),
  );
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
function matchScore(value, minimum) {
  const rounded = value.toFixed(3);
  return value < minimum && Number(rounded) >= minimum
    ? `<${minimum.toFixed(3)}`
    : rounded;
}
function renderGates(recommendation) {
  const details = node("details", "match-reasons");
  details.append(
    node(
      "summary",
      "",
      `Why this suggestion? · similarity ${matchScore(recommendation.score, status.match_threshold)}`,
    ),
  );
  recommendation.gates.forEach((gate) => {
    const value =
      gate.label === "Examples"
        ? `${gate.value} / ${gate.required}`
        : `${matchScore(gate.value, gate.required)} / ${gate.required.toFixed(3)} minimum`;
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
    status: "all",
    tagging: filter,
    include_dismissed: $("include-dismissed").checked,
    offset,
    limit: pageLimit,
    sort: $("review-sort").value,
  });
  if (filter === "tagged") query.set("source", $("tag-source").value);
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
  $("review-total").textContent =
    `${data.photo_total.toLocaleString()} photos · ${data.total.toLocaleString()} animal crops`;
  $("review-view-description").textContent = status.demo
    ? filter === "untagged"
      ? "Demo photos without a simulated pet tag."
      : "Simulated manual and automatic tags. No changes are sent to Immich."
    : filter === "untagged"
      ? "Photos without a pet tag. A suggestion is not a tag; queued tag changes stay here until they sync."
      : "Photos with a pet tag already written to Immich, whether assigned manually or automatically. Select photos to correct or remove tags.";
  $("page-range").textContent = data.total
    ? `${offset + 1}–${Math.min(offset + data.limit, data.total)} of ${data.total.toLocaleString()} animal crops`
    : "No photos here";
  $("previous").disabled = offset === 0;
  $("next").disabled = offset + data.limit >= data.total;
  if (!visibleItems.length) {
    const empty = node("div", "empty");
    empty.append(
      node(
        "h2",
        "",
        filter === "tagged"
          ? "No tagged photos match these filters."
          : "No untagged photos match these filters.",
      ),
      node(
        "p",
        "",
        petFilter || $("tag-source").value !== "all"
          ? "Clear the filters to see the rest of your photos."
          : filter === "tagged"
            ? "Photos move here after their pet tags reach Immich."
            : "New photos will appear here after the next library scan. Use Tagged to check completed tags.",
      ),
      button(
        pets.length ? "Clear filters" : "Register your pet",
        "primary",
        () => {
          if (!pets.length) return switchPanel("pets");
          petFilter = "";
          $("tag-source").value = "all";
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
    const tagged = item.photo_tags.length > 0;
    const syncing =
      item.status === "confirmed" && !item.published && !status.demo;
    const removing = item.status === "rejected" && tagged;
    const tagBadge = node(
      "span",
      "tag-badge " +
        (tagged ? "is-tagged" : syncing ? "is-syncing" : "is-untagged"),
      tagged
        ? `${status.demo ? "Demo tag" : "✓ Tagged"} · ${item.photo_tags.map((tag) => tag.name).join(", ")}`
        : syncing
          ? `Tagging ${item.pet_name}…`
          : "Untagged",
    );
    wrap.append(tagBadge);
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
          item.status === "confirmed"
            ? item.published || status.demo
              ? item.automatic
                ? "Automatic"
                : "Manual"
              : "Syncing"
            : "Skipped",
        ),
      );
    content.append(line, node("p", "filename", item.name));
    if (tagged && item.status === "pending")
      content.append(
        node(
          "p",
          "review-explanation",
          "This photo already has a pet tag. This animal crop has not been assigned yet.",
        ),
      );
    if (removing)
      content.append(
        node("p", "review-explanation", "Removing this pet tag from Immich…"),
      );
    if (rec) {
      const mode = pets.find((pet) => pet.id === rec.pet_id);
      const missed = rec.gates
        .filter((gate) => !gate.passed)
        .map((gate) => gate.label.toLowerCase());
      content.append(
        node(
          "p",
          "review-explanation",
          rec.confident
            ? mode?.auto_tag
              ? "Passes all checks. Waiting for the automatic tagging recheck."
              : `Passes all checks. Automatic tagging is off for ${rec.pet_name}.`
            : `Needs review: ${missed.join(" and ")} below the minimum.`,
        ),
      );
      content.append(renderGates(rec));
    }
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
        (pet) =>
          pet.species === item.species &&
          pet.id !== rec?.pet_id &&
          (item.status !== "confirmed" || pet.id !== item.pet_id),
      );
      if (compatible.length)
        options.append(
          node(
            "span",
            "",
            item.status === "confirmed" ? "Change tag:" : "Tag as:",
          ),
        );
      compatible.forEach((pet) =>
        options.append(
          button(pet.name, "assign-button", () => assign([item], pet)),
        ),
      );
      options.append(
        button(
          item.status === "confirmed" ? "Remove tag" : "Skip",
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
            ? item.automatic
              ? "✓ Automatically tagged in Immich"
              : "✓ Tagged in Immich · human confirmed"
            : status.demo
              ? "Confirmed in demo"
              : item.error
                ? "Tag sync failed · retry in Processing"
                : "Tagging in Immich…",
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
  $("automation-thresholds").textContent =
    `Current checks: ${status.min_references}+ examples · similarity ≥ ${status.match_threshold.toFixed(2)} · lead over look-alikes ≥ ${status.match_margin.toFixed(2)}. These are score cutoffs, not an accuracy percentage.`;
  pets.forEach((pet) => {
    const card = node("article", "pet-card");
    card.append(
      node("h3", "", pet.name),
      node("p", "", `${pet.species} · Immich tag: ${pet.tag}`),
      progressCard(pet),
    );
    card.append(
      node("p", "", `${pet.negative_count} look-alike examples excluded`),
      node(
        "p",
        "",
        `${pet.tagged_count.toLocaleString()} matches tagged in Immich · ${pet.strong_count.toLocaleString()} strong matches waiting${status.suggestions_updating ? " (rechecking…)" : ""}`,
      ),
      button("Review automatic tags →", "secondary", () =>
        showPetReview(pet, "automatic"),
      ),
      button("Review untagged photos →", "quiet", () =>
        showPetReview(pet, "pending"),
      ),
    );
    card.append(
      button("Choose example photos →", "quiet", () => {
        petFilter = "";
        filter = "untagged";
        $("tag-source").value = "all";
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
        filter = "tagged";
        $("tag-source").value = "all";
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
    [
      "Automatic tagging",
      automationSummary() + " · change on Pets & automation",
    ],
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
  $("tag-source").disabled = filter !== "tagged";
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
    $("tag-source").value = "all";
    offset = 0;
    selected.clear();
    updateFilterButtons();
    renderPetFilters();
    renderReview().catch((e) => toast(e.message));
  }),
);
$("tag-source").addEventListener("change", () => {
  offset = 0;
  selected.clear();
  renderReview().catch((e) => toast(e.message));
});
$("include-dismissed").addEventListener("change", () => {
  offset = 0;
  selected.clear();
  renderReview().catch((e) => toast(e.message));
});
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
