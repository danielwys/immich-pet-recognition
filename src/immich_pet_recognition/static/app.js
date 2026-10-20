const $ = (id) => document.getElementById(id);
let token = sessionStorage.getItem("pet-service-token") || "";
let pets = [],
  status = {},
  panel = "home",
  filter = "untagged",
  petFilter = "",
  offset = 0;
let visibleItems = [],
  selected = new Set(),
  objectUrls = [],
  refreshing = false,
  busy = false,
  progressPolling = false;
let reviewRequest = 0,
  pageLimit = 48,
  reviewFingerprint = "";
const titles = {
  home: [
    "Your pets, together.",
    "Photos sort into pet collections automatically. Help with a group when the identity is uncertain.",
  ],
  groups: [
    "A few quick answers.",
    "Name a group once. Clear matches sort themselves; uncertain photos stay for later.",
  ],
  review: [
    "All photos.",
    "Start with untagged photos. Tagged keeps manual and automatic results together.",
  ],
  pets: [
    "Sorting settings.",
    "Every identity you confirm helps future sorting. Choose how much to sort automatically.",
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
  const card = node("div", "pet-sorting-control");
  card.append(
    node(
      "strong",
      pet.auto_tag ? "is-on" : "",
      pet.auto_tag ? "Automatic sorting on" : "Automatic sorting paused",
    ),
  );
  card.append(
    node(
      "p",
      "",
      "Names and corrections are used automatically. No separate teaching step.",
    ),
  );
  card.append(
    button(
      pet.auto_tag ? "Pause sorting" : "Turn on sorting",
      pet.auto_tag ? "secondary" : "primary",
      () => setAutomation(pet),
      busy,
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
  $("include-partial").checked = false;
  offset = 0;
  selected.clear();
  updateFilterButtons();
  renderPetFilters();
  switchPanel("review");
}
function counted(value, noun) {
  return `${value.toLocaleString()} ${noun}${value === 1 ? "" : noun === "match" ? "es" : "s"}`;
}
function recheckActive() {
  return ["queued", "preparing", "running"].includes(status.recheck?.state);
}
function renderStatus() {
  const summary = status.review_summary,
    check = status.recheck,
    number = (value) => (value || 0).toLocaleString();
  $("stat-pending").textContent = number(summary.untagged_photos);

  $("stat-confirmed").textContent = number(summary.tagged_photos);
  $("stat-queued").textContent = number(
    (status.jobs.queued || 0) + (status.jobs.processing || 0),
  );
  $("stat-pets").textContent = pets.length;
  const active = recheckActive(),
    title = $("recheck-title"),
    detail = $("recheck-detail"),
    bar = $("recheck-progress");
  const headings = {
    idle: "Automatic sorting is up to date",
    queued: "Sorting queued",
    preparing: "Learning from your identifications…",
    running: `Sorting photos — ${check.percent ?? 0}%`,
    complete: "Sorting complete · 100%",
    failed: "Sorting stopped — retry needed",
  };
  title.textContent = headings[check.state] || headings.idle;
  detail.textContent =
    check.state === "failed"
      ? `${number(check.completed)} of ${number(check.total)} animal crops checked. ${check.error}`
      : check.state === "queued"
        ? "Waiting for the current background step to finish. Your latest identifications will be used."
        : check.state === "preparing"
          ? "Checking the remaining photos against the identities you confirmed."
          : check.state === "running"
            ? `${number(check.completed)} of ${number(check.total)} animal crops checked · ${number(check.automatic_matches)} matched for automatic tagging so far.`
            : check.state === "complete"
              ? `${counted(check.completed, "animal crop")} checked · ${number(check.automatic_matches)} matched for automatic tagging · Finished ${new Date(check.finished_at).toLocaleString()}.`
              : "Clear matches go into pet collections automatically. Uncertain photos are grouped for a quick answer.";
  bar.classList.toggle("hidden", check.state === "idle");
  if (check.percent === null || check.percent === undefined)
    bar.removeAttribute("value");
  else bar.value = check.percent;
  $("recheck-now").textContent =
    check.state === "failed" ? "Retry sorting" : "Sort again";
  $("recheck-now").disabled = active || busy;
  $("rematch").disabled = active || busy;
  document
    .querySelector(".recheck-card")
    .classList.toggle("is-failed", check.state === "failed");
  const remaining = `${counted(summary.untagged_photos, "untagged photo")} ${summary.untagged_photos === 1 ? "remains" : "remain"}`;
  $("recheck-next").textContent = active
    ? `${remaining}. Clear matches will appear in your pet collections automatically.`
    : check.state === "failed"
      ? "Sorting stopped before finishing. Retry to continue."
      : summary.untagged_photos
        ? `${remaining}. Review similar groups when you want to help with the uncertain ones.`
        : "No completely untagged photos left to review.";
  $("tag-sync-status").classList.toggle("hidden", !summary.pending_tag_changes);
  $("tag-sync-status").textContent =
    `${number(summary.pending_tag_changes)} tag changes waiting to sync to Immich. Tag sync is separate from the recheck.`;
  $("review-breakdown").classList.toggle(
    "hidden",
    !summary.untagged_crops && !summary.extra_pending_crops,
  );
  $("review-count-explanation").textContent =
    `${number(summary.untagged_photos)} completely untagged photos contain ${number(summary.untagged_crops)} animal crops. A photo may have several crops.${summary.extra_pending_crops ? ` Another ${number(summary.extra_pending_crops)} unassigned crops are in ${number(summary.partially_tagged_photos)} photos that already have a pet tag; they are excluded from the main count.` : ""}`;
  const reasons = $("review-reasons");
  reasons.replaceChildren();
  const labels = {
    no_candidate: "no identity found yet",
    needs_examples: "still establishing this identity",
    below_similarity: "different from known appearances",
    ambiguous: "could be more than one pet",
    automation_paused: "pass the checks, but automatic tagging is off",
    ready: "pass the checks; awaiting automatic processing",
  };
  Object.entries(labels).forEach(([key, label]) => {
    if (!summary.reasons[key]) return;
    const item = node("span", "review-reason");
    item.append(
      node("strong", "", number(summary.reasons[key])),
      `animal crops: ${label}`,
    );
    reasons.append(item);
  });
  $("review-next-step").textContent = active
    ? "These reasons use the last stored scores and will update during the recheck. Each crop is counted under its first unmet check."
    : check.state === "failed"
      ? "These reasons are incomplete until the recheck finishes. Retry above."
      : "Review a group once instead of confirming each photo. Every identity or correction you give helps future sorting automatically.";
  renderUndo();
  $("suggestion-state").textContent =
    filter === "tagged"
      ? "Completed pet tags in Immich. Select photos to change or remove a tag."
      : "Choose a pet to review its suggestions, or select a batch to assign together.";
}
async function refresh() {
  if (refreshing || busy || progressPolling || $("group-dialog").open) return;
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
    renderStatus();
    $("last-sync").textContent = status.last_sync
      ? "Last sync · " + new Date(status.last_sync).toLocaleString()
      : "Waiting for first sync";
    $("tagging-state").textContent = automationSummary();
    renderPetFilters();
    updateSelection();
    if (!$("login-dialog").open && !(panel === "review" && selected.size))
      await renderPanel();
  } finally {
    refreshing = false;
  }
}
async function renderPanel() {
  if (panel === "home") await renderHome();
  if (panel === "groups") await renderGroups();
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
    chosen.some((item) => item.published || !item.recommendation);
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
        busy || !chosen.length,
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
      `${result.saved} saved${result.failed ? ` · ${result.failed} could not be saved: ${failures[0].error}` : ""}. ${endpoint === "references/batch" || items.some((item) => item.status === "confirmed") ? "Sorting is updating from your feedback." : ""}${status.demo ? " Demo only; no Immich tags written." : endpoint === "review/batch" ? " Immich tag changes queued." : ""}`;
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
  return submitBatch(
    items.map((item) => ({
      detection_id: item.id,
      status: "confirmed",
      pet_id: pet.id,
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
  details.append(node("summary", "", "Match details"));
  recommendation.gates
    .filter((gate) => gate.label !== "Examples")
    .forEach((gate) => {
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
    tagging:
      filter === "untagged" && $("include-partial").checked
        ? "unassigned"
        : filter,
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
      ? $("include-partial").checked
        ? "Unassigned animals from all photos, including photos that already have another pet tagged."
        : "Photos without a pet tag. A suggestion is not a tag; queued tag changes stay here until they sync."
      : `${petFilter ? pets.find((pet) => pet.id === petFilter)?.name + "’s completed tags" : "Completed pet tags"} in Immich, manual and automatic together. Use Change tag or Remove tag to make a correction.`;
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
    const rec = filter === "tagged" ? null : item.recommendation;
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
    const speciesLabel = node(
      "span",
      "species",
      item.status === "confirmed" ? item.species : "Detected: " + item.species,
    );
    speciesLabel.title =
      "Assigning a pet also corrects this crop’s species if the detector got it wrong.";
    wrap.append(img, speciesLabel, checkbox);
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
            ? pets.length && !pets.some((pet) => pet.species === item.species)
              ? "Check detected type"
              : pets.some(
                    (pet) =>
                      pet.species === item.species && pet.positive_count > 0,
                  )
                ? "Unmatched"
                : "Choose a pet"
            : rec.confident
              ? "Strong suggestion"
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
          pet.id !== rec?.pet_id &&
          (item.status !== "confirmed" || pet.id !== item.pet_id),
      );
      const choices =
        item.status === "confirmed"
          ? node("details", "tag-corrections")
          : options;
      if (compatible.length) {
        if (item.status === "confirmed") {
          choices.append(node("summary", "", "Change tag"));
          options.append(choices);
        } else options.append(node("span", "", "Tag as:"));
      }
      compatible.forEach((pet) =>
        choices.append(
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
      if (
        item.status !== "confirmed" &&
        pets.length &&
        !pets.some((pet) => pet.species === item.species)
      ) {
        content.append(
          node(
            "p",
            "review-explanation",
            "Wrong animal type? Choose the right pet above; its species will replace the detected label.",
          ),
        );
      }
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
  $("sorting-profile").value = status.matching.profile;
  $("sorting-mode-label").textContent =
    status.matching.mode === "adaptive"
      ? "Learns from your identifications"
      : "Legacy fixed matching is active";
  $("sorting-mode-message").textContent =
    status.matching.mode === "adaptive"
      ? "Each pet has its own matching boundary, updated automatically from your feedback."
      : "Save a preference to replace the old fixed cutoff with learned matching.";
  pets.forEach((pet) => {
    const card = node("article", "pet-card");
    card.append(
      node("h3", "", pet.name),
      progressCard(pet),
      button("Open collection →", "quiet", () => showPetReview(pet, "tagged")),
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
      automationSummary() + " · change in Sorting settings",
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
      "Recognition policy",
      status.matching.mode === "adaptive"
        ? "Learns per pet from your confirmed identities"
        : "Legacy fixed matching",
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
  if (status.review_summary) renderStatus();
  $("tag-source").disabled = filter !== "tagged";
  $("include-partial").disabled = filter !== "untagged";
  document.querySelector('#review-sort option[value="suggested"]').hidden =
    filter === "tagged";
  if (filter === "tagged" && $("review-sort").value === "suggested")
    $("review-sort").value = "newest";
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
    $("include-partial").checked = false;
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
$("include-partial").addEventListener("change", () => {
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
    })),
  ),
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
async function requestRecheck() {
  if (busy || recheckActive()) return;
  busy = true;
  renderStatus();
  try {
    await api("rematch", { method: "POST" });
    toast("Sorting queued. Follow its progress above.");
  } catch (e) {
    toast(e.message);
  } finally {
    busy = false;
    await refresh();
  }
}
$("rematch").addEventListener("click", requestRecheck);
$("recheck-now").addEventListener("click", requestRecheck);
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
    toast("Pet registered. Name a matching group to start recognizing them.");
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
let workspaceRequest = 0,
  homeFingerprint = "",
  homeGroupFingerprint = "",
  groupFingerprint = "";
let groupPetFilter = "",
  groupOffset = 0,
  activeGroup = null,
  includedGroupPhotos = new Set();
function releaseCrops() {
  objectUrls.forEach(URL.revokeObjectURL);
  objectUrls = [];
}
function renderUndo() {
  const action = status.undo_group;
  $("group-undo").classList.toggle("hidden", !action);
  if (!action) return;
  const pet = pets.find((pet) => pet.id === action.pet_id);
  $("group-undo-label").textContent = action.skip
    ? "Last group hidden from review."
    : `Last group added to ${pet?.name || "a pet"}.`;
  $("undo-group").disabled = busy;
}
function groupCard(group) {
  const card = node("article", "identity-group");
  card.dataset.groupId = group.id;
  const mosaic = node("button", "group-mosaic");
  mosaic.setAttribute(
    "aria-label",
    `Review ${counted(group.photo_count, "photo")} ${group.pet_name ? "suggested as " + group.pet_name : "without a name"}`,
  );
  mosaic.addEventListener("click", () =>
    openGroup(group).catch((e) => toast(e.message)),
  );
  group.samples.forEach((sample) => {
    const image = node("img");
    image.alt = sample.name;
    image.loading = "lazy";
    mosaic.append(image);
    loadCrop(sample.id, image).catch(() => {});
  });
  const content = node("div", "group-content");
  content.append(
    node(
      "span",
      "group-size",
      `${counted(group.photo_count, "photo")}${group.crop_count !== group.photo_count ? ` · ${group.crop_count} animal crops` : ""}`,
    ),
  );
  content.append(
    node(
      "h3",
      "",
      group.pet_name
        ? `${group.crop_count === 1 ? "Is this" : "Are these"} ${group.pet_name}?`
        : "Who is in this group?",
    ),
  );
  const actions = node("div", "group-actions");
  if (group.pet_id)
    actions.append(
      button(
        `${group.crop_count === 1 ? "Yes, this is" : "Yes, all are"} ${group.pet_name}`,
        "primary",
        () => decideGroup(group, group.pet_id),
      ),
    );
  else
    pets.forEach((pet) =>
      actions.append(
        button(pet.name, "secondary", () => decideGroup(group, pet.id)),
      ),
    );
  content.append(
    actions,
    button("Review / remove exceptions", "group-review-link quiet", () =>
      openGroup(group),
    ),
  );
  if (group.pet_id) {
    const alternatives = node("details", "group-alternatives");
    alternatives.append(node("summary", "", "Choose another pet"));
    pets
      .filter((pet) => pet.id !== group.pet_id)
      .forEach((pet) =>
        alternatives.append(
          button(pet.name, "secondary", () => decideGroup(group, pet.id)),
        ),
      );
    content.append(alternatives);
  }
  content.append(
    button("Not one of my pets", "quiet", () =>
      decideGroup(group, null, [], true),
    ),
  );
  card.append(mosaic, content);
  return card;
}
function groupsPlaceholder(grid, updating) {
  const empty = node("div", "group-empty");
  empty.append(
    node(
      "h3",
      "",
      updating
        ? "Sorting is working on these photos…"
        : "No groups need a decision here.",
    ),
    node(
      "p",
      "",
      updating
        ? "Clear matches go straight into pet collections. The remaining groups will appear when this pass finishes."
        : "Open a pet collection to browse or correct results.",
    ),
  );
  grid.append(empty);
}
async function renderHome() {
  const request = ++workspaceRequest;
  const [collections, groups] = await Promise.all([
    api("collections"),
    api("groups?limit=3"),
  ]);
  if (panel !== "home" || request !== workspaceRequest) return;
  $("nav-count").textContent = groups.updating
    ? "…"
    : groups.total_groups.toLocaleString();
  const fingerprint = JSON.stringify([collections, status.matching]);
  if (fingerprint !== homeFingerprint) {
    homeFingerprint = fingerprint;
    releaseCrops();
    const grid = $("collection-grid");
    grid.replaceChildren();
    collections.forEach((pet) => {
      const card = node("button", "collection-card");
      card.setAttribute("aria-label", `Open ${pet.name} collection`);
      const image = node("img");
      image.alt = pet.name;
      if (pet.cover_id) {
        card.append(image);
        loadCrop(pet.cover_id, image).catch(() => {});
      } else card.append(node("div", "collection-placeholder", pet.name[0]));
      const caption = node("div", "collection-caption");
      caption.append(
        node("h2", "", pet.name),
        node("strong", "", counted(pet.photo_count, "photo")),
        node(
          "p",
          "",
          `${pet.auto_photo_count.toLocaleString()} sorted automatically`,
        ),
        node(
          "span",
          pet.auto_tag ? "collection-mode on" : "collection-mode",
          pet.auto_tag ? "Automatic sorting on" : "Automatic sorting paused",
        ),
      );
      if (pet.queued_count)
        caption.append(
          node(
            "p",
            "collection-sync",
            `${counted(pet.queued_count, "match")} syncing to Immich…`,
          ),
        );
      card.append(caption);
      card.addEventListener("click", () => showPetReview(pet, "tagged"));
      grid.append(card);
    });
    if (!collections.length)
      grid.append(
        button("Name your first pet", "collection-add", () =>
          switchPanel("pets"),
        ),
      );
    let hint = $("home-sorting-hint");
    if (!hint) {
      hint = node("div", "home-sorting-hint");
      hint.id = "home-sorting-hint";
      grid.after(hint);
    }
    hint.replaceChildren();
    if (status.matching.mode === "fixed") {
      hint.append(
        node(
          "p",
          "",
          "Replace the old fixed cutoff with matching that adapts to your confirmed identities.",
        ),
        button("Start automatic sorting", "primary", () =>
          saveSorting("balanced"),
        ),
      );
    } else {
      hint.append(
        node(
          "p",
          "",
          "Names and corrections improve sorting automatically. You only need to help with uncertain groups.",
        ),
      );
    }
  }
  $("home-group-summary").textContent = groups.updating
    ? "Updating groups from your latest identifications."
    : `${counted(groups.total_groups, "group")} of similar photos. One answer can sort the whole group.`;
  const groupState = JSON.stringify(groups);
  if (groupState === homeGroupFingerprint) return;
  homeGroupFingerprint = groupState;
  const reviews = $("home-groups");
  reviews.replaceChildren();
  groups.items.forEach((group) => reviews.append(groupCard(group)));
  if (!groups.items.length) groupsPlaceholder(reviews, groups.updating);
}
async function renderGroups() {
  const request = ++workspaceRequest;
  const query = new URLSearchParams({ offset: groupOffset, limit: 12 });
  if (groupPetFilter) query.set("pet_id", groupPetFilter);
  const groups = await api("groups?" + query);
  if (panel !== "groups" || request !== workspaceRequest) return;
  if (
    !groups.updating &&
    groups.total_groups &&
    groupOffset >= groups.total_groups
  ) {
    groupOffset = Math.floor((groups.total_groups - 1) / 12) * 12;
    return renderGroups();
  }
  const filters = $("group-pet-filters");
  filters.replaceChildren();
  [["", "All pets"], ...pets.map((pet) => [pet.id, pet.name])].forEach(
    ([id, name]) => {
      const control = button(
        name,
        "pet-chip" + (id === groupPetFilter ? " active" : ""),
        () => {
          groupPetFilter = id;
          groupOffset = 0;
          groupFingerprint = "";
          return renderGroups();
        },
      );
      control.setAttribute("aria-pressed", String(id === groupPetFilter));
      filters.append(control);
    },
  );
  $("group-count").textContent = groups.updating
    ? "Updating…"
    : `${counted(groups.total_groups, "group")} · ${counted(groups.total_photos, "photo")}`;
  $("nav-count").textContent = groupPetFilter
    ? $("nav-count").textContent
    : groups.updating
      ? "…"
      : groups.total_groups.toLocaleString();
  $("group-page").textContent = groups.total_groups
    ? `${groupOffset + 1}–${Math.min(groupOffset + 12, groups.total_groups)} of ${groups.total_groups.toLocaleString()} groups`
    : "";
  $("group-previous").disabled = groupOffset === 0 || busy;
  $("group-next").disabled = groupOffset + 12 >= groups.total_groups || busy;
  const fingerprint = query.toString() + JSON.stringify(groups);
  if (fingerprint === groupFingerprint) return;
  groupFingerprint = fingerprint;
  releaseCrops();
  const grid = $("group-grid");
  grid.replaceChildren();
  groups.items.forEach((group) => grid.append(groupCard(group)));
  if (!groups.items.length) groupsPlaceholder(grid, groups.updating);
}
async function openGroup(group) {
  if (busy) return;
  activeGroup = await api("groups/" + group.id);
  includedGroupPhotos = new Set(activeGroup.members.map((row) => row.id));
  $("group-dialog-title").textContent = group.pet_name
    ? `Are these ${group.pet_name}?`
    : "Name this group";
  const grid = $("group-photo-grid");
  grid.replaceChildren();
  activeGroup.members.forEach((row) => {
    const label = node("label", "group-photo");
    const image = node("img");
    image.alt = row.name;
    image.loading = "lazy";
    const check = node("input");
    check.type = "checkbox";
    check.checked = true;
    check.setAttribute("aria-label", "Include " + row.name);
    check.addEventListener("change", () => {
      if (check.checked) includedGroupPhotos.add(row.id);
      else includedGroupPhotos.delete(row.id);
      updateGroupSelection();
    });
    label.append(image, check, node("span", "", row.name));
    grid.append(label);
    loadCrop(row.id, image).catch(() => {});
  });
  updateGroupSelection();
  $("group-dialog").showModal();
}
function updateGroupSelection() {
  if (!activeGroup) return;
  const photos = new Set(
    activeGroup.members
      .filter((row) => includedGroupPhotos.has(row.id))
      .map((row) => row.asset_id),
  );
  $("group-selected-count").textContent =
    `${counted(photos.size, "photo")} selected`;
  $("group-toggle-all").textContent =
    includedGroupPhotos.size === activeGroup.members.length
      ? "Clear selection"
      : "Select all";
  const actions = $("group-dialog-pets");
  actions.replaceChildren();
  pets.forEach((pet) =>
    actions.append(
      button(
        `${includedGroupPhotos.size === 1 ? "This is" : "These are"} ${pet.name}`,
        pet.id === activeGroup.pet_id ? "primary" : "secondary",
        () =>
          decideGroup(
            activeGroup,
            pet.id,
            activeGroup.members
              .filter((row) => !includedGroupPhotos.has(row.id))
              .map((row) => row.id),
          ),
        busy || !photos.size,
      ),
    ),
  );
}
async function decideGroup(group, petId, excluded = [], skip = false) {
  if (busy) return;
  busy = true;
  document
    .querySelectorAll(".identity-group button, .group-dialog button")
    .forEach((control) => {
      control.disabled = true;
    });
  try {
    const result = await api(`groups/${group.id}/decision`, {
      method: "POST",
      body: JSON.stringify({ pet_id: petId, excluded, skip }),
    });
    if ($("group-dialog").open) $("group-dialog").close();
    const name = pets.find((pet) => pet.id === petId)?.name;
    toast(
      skip
        ? "Group hidden. Undo is available below."
        : `Group added to ${name}. Sorting is updating automatically.`,
    );
    homeFingerprint = "";
    homeGroupFingerprint = "";
    groupFingerprint = "";
  } catch (error) {
    toast(error.message);
  } finally {
    busy = false;
    document
      .querySelectorAll(".identity-group button, .group-dialog button")
      .forEach((control) => {
        control.disabled = false;
      });
    updateGroupSelection();
    await refresh();
  }
}
async function saveSorting(profile) {
  if (busy) return;
  busy = true;
  $("save-sorting").disabled = true;
  try {
    await api("matching", {
      method: "PUT",
      body: JSON.stringify({ mode: "adaptive", profile }),
    });
    homeFingerprint = "";
    homeGroupFingerprint = "";
    groupFingerprint = "";
    toast("Sorting preference saved. Checking your library automatically.");
  } catch (error) {
    toast(error.message);
  } finally {
    busy = false;
    $("save-sorting").disabled = false;
    await refresh();
  }
}
$("save-sorting").addEventListener("click", () =>
  saveSorting($("sorting-profile").value),
);
$("group-previous").addEventListener("click", () => {
  groupOffset = Math.max(0, groupOffset - 12);
  renderGroups().catch((e) => toast(e.message));
});
$("group-next").addEventListener("click", () => {
  groupOffset += 12;
  renderGroups().catch((e) => toast(e.message));
});
$("close-group").addEventListener("click", () => {
  if (!busy) $("group-dialog").close();
});
$("group-dialog").addEventListener("cancel", (event) => {
  if (busy) event.preventDefault();
});
$("group-toggle-all").addEventListener("click", () => {
  if (!activeGroup || busy) return;
  includedGroupPhotos =
    includedGroupPhotos.size === activeGroup.members.length
      ? new Set()
      : new Set(activeGroup.members.map((row) => row.id));
  $("group-photo-grid")
    .querySelectorAll("input")
    .forEach((check, index) => {
      check.checked = includedGroupPhotos.has(activeGroup.members[index].id);
    });
  updateGroupSelection();
});
$("undo-group").addEventListener("click", async () => {
  if (busy || !status.undo_group) return;
  busy = true;
  try {
    await api("groups/undo/" + status.undo_group.id, { method: "POST" });
    homeFingerprint = "";
    homeGroupFingerprint = "";
    groupFingerprint = "";
    toast(
      "Group change undone. Its automatic follow-on matches were restored too.",
    );
  } catch (error) {
    toast(error.message);
  } finally {
    busy = false;
    await refresh();
  }
});

refresh().catch((e) => {
  if (!$("login-dialog").open) toast(e.message);
});
setInterval(() => {
  if (!$("login-dialog").open && !document.hidden && !busy)
    refresh().catch(() => {});
}, 10000);

// Only status changes during this lightweight poll; selections and photo cards stay put.
setInterval(async () => {
  if (
    !recheckActive() ||
    progressPolling ||
    refreshing ||
    busy ||
    document.hidden ||
    $("login-dialog").open
  )
    return;
  progressPolling = true;
  try {
    status = await api("status");
    renderStatus();
    if (!recheckActive()) {
      progressPolling = false;
      await refresh();
    }
  } catch {
    // The regular refresh handles connection errors and authentication.
  } finally {
    progressPolling = false;
  }
}, 2000);
