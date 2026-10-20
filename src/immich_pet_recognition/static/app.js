const $ = (id) => document.getElementById(id);
let token = sessionStorage.getItem("pet-service-token") || "";
let pets = [],
  status = {},
  view = null,
  routeVersion = 0,
  polling = false;
let selected = new Set(),
  selectMode = false,
  lastSelected = -1,
  viewerIndex = 0;
let detailGroup = null,
  detailSelected = new Set(),
  lastUndo = null;
let dismissedUndoId = sessionStorage.getItem("pet-dismissed-undo") || "";
let boardGeneration = 0,
  unidentifiedCount;
const removedGroups = new Set(),
  removedPhotos = new Set(),
  libraryPreviews = new Map();
const number = (value) => (value || 0).toLocaleString();
const plural = (count, word) =>
  `${number(count)} ${word}${count === 1 ? "" : "s"}`;
const petById = (id) => pets.find((pet) => pet.id === id);
function el(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function button(text, className, action, label) {
  const element = el("button", className, text);
  if (label) element.setAttribute("aria-label", label);
  element.addEventListener("click", (event) => {
    event.stopPropagation();
    Promise.resolve(action(event)).catch(showError);
  });
  return element;
}
function link(text, href, className) {
  const element = el("a", className, text);
  element.href = href;
  return element;
}
async function api(path, options = {}) {
  const response = await fetch("/api/" + path, {
    ...options,
    priority: options.method ? "high" : "auto",
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
  const body = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : "This could not be saved. Please try again.",
    );
  return body;
}
function toast(message) {
  $("toast").textContent = message;
  $("toast").classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $("toast").classList.add("hidden"), 4200);
}
function showError(error) {
  toast(error.message || String(error));
}

// Only visible thumbnails enter the two-request image lane. Decisions never
// wait behind a page of full-size crops, and revisiting a photo reuses its URL.
const cropCache = new Map(),
  cropRequests = new Map(),
  cropQueue = [];
let loadingImages = 0;
const imageObserver = new IntersectionObserver(
  (entries) => {
    entries.forEach(({ isIntersecting, target }) => {
      if (!isIntersecting) return;
      imageObserver.unobserve(target);
      scheduleCrop(
        target,
        target.dataset.cropId,
        Number(target.dataset.cropSize),
        target.dataset.mediaKind,
      );
    });
  },
  { rootMargin: "300px" },
);
function image(
  id,
  alt,
  className = "",
  size = 320,
  immediate = false,
  media = "crop",
) {
  const img = el("img", className);
  img.alt = alt || "";
  img.decoding = "async";
  img.dataset.cropId = id;
  img.dataset.mediaKind = media;
  img.dataset.cropSize = size;
  if (id) {
    if (immediate) scheduleCrop(img, id, size, media);
    else imageObserver.observe(img);
  }
  return img;
}
function scheduleCrop(img, id, size, media = "crop") {
  const key = `${media}:${id}:${size}`;
  const cached = cropCache.get(key);
  if (cached) {
    cropCache.delete(key);
    cropCache.set(key, cached);
    img.src = cached;
    return;
  }
  if (cropRequests.has(key)) {
    cropRequests.get(key).push(img);
    return;
  }
  cropRequests.set(key, [img]);
  if (size > 640) cropQueue.unshift({ key, id, size, media });
  else cropQueue.push({ key, id, size, media });
  drainCrops();
}
function drainCrops() {
  while (loadingImages < 2 && cropQueue.length) {
    const task = cropQueue.shift();
    const waiting = cropRequests
      .get(task.key)
      .filter((img) => img.isConnected || img.id === "viewer-image");
    if (!waiting.length) {
      cropRequests.delete(task.key);
      continue;
    }
    loadingImages++;
    fetch(
      `/api/detections/${task.id}/${task.media === "photo" ? "photo" : "crop"}?size=${task.size}`,
      {
        priority: "low",
        headers: token ? { Authorization: "Bearer " + token } : {},
      },
    )
      .then(async (response) => {
        if (!response.ok) throw new Error("Photo unavailable");
        const url = URL.createObjectURL(await response.blob());
        cropCache.set(task.key, url);
        (cropRequests.get(task.key) || []).forEach((img) => {
          if (img.isConnected) img.src = url;
        });
        if (cropCache.size > 350) {
          for (const [key, oldUrl] of cropCache) {
            if (![...document.images].some((img) => img.src === oldUrl)) {
              URL.revokeObjectURL(oldUrl);
              cropCache.delete(key);
              break;
            }
          }
        }
      })
      .catch(() => {})
      .finally(() => {
        cropRequests.delete(task.key);
        loadingImages--;
        drainCrops();
      });
  }
}
function photoImage(row, size = 320) {
  return image(row.id, row.name, "", size, false, "photo");
}
function assignmentsFor(item) {
  return item.assignment_ids || [item.id];
}
function photoFailed(item, failedIds) {
  return assignmentsFor(item).some((id) => failedIds.has(id));
}
function operationId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return [...crypto.getRandomValues(new Uint8Array(16))]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}
function keyOf(item) {
  return item.type === "group" ? "g:" + item.id : "d:" + item.id;
}
function photoIds(items) {
  return new Set(
    items.flatMap((item) =>
      item.type === "group"
        ? item.asset_ids || item.samples.map((row) => row.asset_id)
        : [item.asset_id],
    ),
  );
}
function selectionItems() {
  return (view?.items || []).filter((item) => selected.has(keyOf(item)));
}
function navigate(hash) {
  location.hash = hash;
}
function route() {
  const parts = (location.hash.slice(1) || "library").split("/");
  if (parts[0] === "pet" && petById(parts[1]))
    return {
      type: "album",
      petId: parts[1],
      candidates: parts[2] === "more",
      key: parts.join("/"),
    };
  if (parts[0] === "unidentified")
    return {
      type: "unidentified",
      petId: parts[1] || "",
      key: parts.join("/"),
    };
  if (parts[0] === "photos")
    return { type: "album", petId: "", candidates: false, key: "photos" };
  if (["settings", "activity"].includes(parts[0]))
    return { type: parts[0], key: parts[0] };
  return { type: "library", key: "library" };
}
function heading(title, subtitle) {
  const container = el("div", "page-heading"),
    text = el("div");
  text.append(el("h1", "", title));
  if (subtitle) text.append(el("p", "", subtitle));
  container.append(text);
  $("view").append(container);
  return container;
}
function empty(container, title, detail) {
  const message = el("div", "empty");
  message.append(
    el("div", "empty-mark", "◌"),
    el("h2", "", title),
    el("p", "", detail),
  );
  container.append(message);
}
function renderNavigation() {
  const nav = $("pet-navigation");
  nav.replaceChildren();
  pets.forEach((pet) => {
    const a = link("", "#pet/" + pet.id, "side-link");
    a.append(
      image(pet.cover_id, "", "nav-avatar", 160),
      el("span", "", pet.name),
      el("small", "nav-number", number(pet.photo_count)),
    );
    nav.append(a);
  });
  $("unidentified-count").textContent = number(
    status.review_summary?.untagged_photos,
  );
  document.querySelectorAll(".side-link,.mobile-navigation a").forEach((a) => {
    const current = location.hash || "#library";
    a.classList.toggle(
      "active",
      a.hash === current ||
        (a.hash.startsWith("#pet/") && current.startsWith(a.hash)) ||
        (a.hash === "#unidentified" && current.startsWith(a.hash)),
    );
  });
}
function renderActivity() {
  const check = status.recheck || {},
    queued = outbox.pending;
  const active = ["queued", "preparing", "running"].includes(check.state);
  const syncing = status.review_summary?.pending_tag_changes || 0;
  const failed =
    outbox.failed.length || status.last_error || check.state === "failed";
  $("activity-label").textContent = queued
    ? `Saving ${plural(queued, "change")}`
    : failed
      ? "Needs attention"
      : active
        ? `Organizing${check.percent != null ? " · " + check.percent + "%" : "…"}`
        : syncing
          ? `Syncing ${plural(syncing, "photo")}`
          : "Up to date";
  $("activity-indicator").classList.toggle(
    "working",
    Boolean(queued || active || syncing),
  );
  $("activity-indicator").classList.toggle("failed", Boolean(failed));
  $("demo-banner").classList.toggle("hidden", !status.demo);
  $("error-banner").classList.toggle("hidden", !status.last_error);
  $("error-banner").textContent = status.last_error || "";
  renderUndo();
}
async function refreshStatus() {
  if (polling) return;
  polling = true;
  try {
    const [nextStatus, nextPets, groups] = await Promise.all([
      api("status"),
      api("collections"),
      api("groups?limit=1"),
    ]);
    unidentifiedCount = groups.total_photos;
    status = nextStatus;
    pets = nextPets;
    if (
      !lastUndo &&
      status.undo_group &&
      status.undo_group.id !== dismissedUndoId
    )
      lastUndo = {
        kind: "group",
        id: status.undo_group.id,
        label: status.undo_group.skip
          ? "Photos hidden"
          : "Added to " + (petById(status.undo_group.pet_id)?.name || "pet"),
      };
    renderNavigation();
    updateAlbumCount();
    renderActivity();
    if (view?.type === "activity") renderActivityPage();
  } finally {
    polling = false;
  }
}
let albumsRefreshing = false;
function updateAlbumCount() {
  const count = document.querySelector(".album-title p");
  if (count && view?.petId && petById(view.petId))
    count.textContent = plural(petById(view.petId).photo_count, "photo");
}
async function refreshAlbums() {
  if (albumsRefreshing) return;
  albumsRefreshing = true;
  try {
    pets = await api("collections");
    renderNavigation();
    updateAlbumCount();
  } finally {
    albumsRefreshing = false;
  }
}
async function openRoute() {
  const version = ++routeVersion;
  selected.clear();
  selectMode = false;
  lastSelected = -1;
  view = {
    ...route(),
    items: [],
    total: 0,
    offset: 0,
    loading: false,
    source: "all",
  };
  $("view").replaceChildren();
  renderSelection();
  renderNavigation();
  const current = view;
  if (current.type === "library") await renderLibrary(version);
  else if (current.type === "album") {
    renderAlbumShell();
    await loadItems(false, version);
  } else if (current.type === "unidentified") {
    renderUnidentifiedShell();
    await loadItems(false, version);
  } else if (current.type === "settings") renderSettings();
  else if (current.type === "activity") renderActivityPage();
  if (version === routeVersion)
    window.scrollTo({ top: 0, behavior: "instant" });
}
async function renderLibrary(version) {
  const top = heading("Library");
  top.append(link("All pet photos", "#photos", "text-button"));
  const covers = el("div", "pet-covers");
  $("view").append(covers);
  pets.forEach((pet) => {
    const cover = button(
      "",
      "pet-cover",
      () => navigate("pet/" + pet.id),
      `Open ${pet.name}`,
    );
    if (pet.cover_id) cover.append(image(pet.cover_id, pet.name, "", 640));
    else cover.append(el("div", "cover-initial", pet.name[0]));
    const caption = el("div", "cover-caption");
    caption.append(
      el("h2", "", pet.name),
      el("p", "", plural(pet.photo_count, "photo")),
    );
    cover.append(caption);
    covers.append(cover);
  });
  if (!pets.length) {
    empty(
      covers,
      "Meet your first pet",
      "Add a name to start collecting their photos.",
    );
    covers.append(link("Add a pet", "#settings", "primary"));
  }
  const unknown = el("div", "section-title");
  unknown.append(
    el("h2", "", "Unidentified"),
    link("Browse photos →", "#unidentified", "text-button"),
  );
  $("view").append(unknown);
  const line = el(
    "p",
    "photo-caption-note",
    "A place for photos without a name. Organize them whenever you like.",
  );
  $("view").append(line);
  const unknownStrip = el("div", "photo-strip");
  $("view").append(unknownStrip);
  const unknownPromise = api("groups?limit=8").then((data) => {
    if (version !== routeVersion) return;
    unidentifiedCount = data.total_photos;
    $("unidentified-count").textContent = number(unidentifiedCount);
    data.items.slice(0, 8).forEach((group) => {
      const tile = button(
        "",
        "photo-tile",
        () => navigate("unidentified"),
        `Browse ${plural(group.photo_count, "unidentified photo")}`,
      );
      tile.append(image(group.samples[0]?.id, group.samples[0]?.name));
      if (group.photo_count > 1)
        tile.append(el("span", "tile-count", group.photo_count + " photos"));
      unknownStrip.append(tile);
    });
    if (!data.items.length)
      line.textContent = "All your detected pet photos have a name.";
  });
  const strips = pets.map(async (pet) => {
    const section = el("div", "section-title");
    section.append(
      el("h2", "", pet.name),
      link("View album →", "#pet/" + pet.id, "text-button"),
    );
    const strip = el("div", "photo-strip");
    $("view").append(section, strip);
    const data = await api(
      `detections?status=confirmed&tagging=all&pet_id=${pet.id}&sort=newest&limit=10&unique_photos=true`,
    );
    if (version !== routeVersion) return;
    libraryPreviews.set(pet.id, data.items);
    data.items.forEach((row, index) => {
      const tile = button(
        "",
        "photo-tile",
        () => openViewer(data.items, index),
        `Open ${row.name}`,
      );
      tile.append(photoImage(row));
      strip.append(tile);
    });
  });
  await Promise.allSettled([unknownPromise, ...strips]);
}
function renderAlbumShell() {
  const pet = petById(view.petId),
    top = el("div", "page-heading");
  const identity = el("div", "album-heading");
  if (pet) identity.append(image(pet.cover_id, "", "album-avatar", 160));
  const title = el("div", "album-title");
  title.append(
    el("h1", "", pet?.name || "All pet photos"),
    el(
      "p",
      "",
      pet ? plural(pet.photo_count, "photo") : "Your named pets, together",
    ),
  );
  if (pet) {
    const nameHeading = title.querySelector("h1");
    const switcher = el("div", "album-switcher");
    nameHeading.replaceWith(switcher);
    const chevron = el("span", "album-chevron", "⌄");
    chevron.setAttribute("aria-hidden", "true");
    switcher.append(nameHeading, chevron);
    const picker = el("select", "album-pet-picker");
    picker.setAttribute("aria-label", "Switch pet");
    pets.forEach((other) => {
      const option = el("option", "", other.name);
      option.value = other.id;
      picker.append(option);
    });
    picker.value = pet.id;
    picker.addEventListener("change", () =>
      navigate("pet/" + picker.value + (view.candidates ? "/more" : "")),
    );
    switcher.append(picker);
  }
  identity.append(title);
  top.append(identity);
  if (!view.candidates && view.petId) {
    const select = button("Select", "secondary", () => {
      selectMode = !selectMode;
      selected.clear();
      renderGrid();
      renderSelection();
      select.textContent = selectMode ? "Done" : "Select";
    });
    select.id = "album-select";
    top.append(select);
  }
  $("view").append(top);

  if (pet) {
    const tabs = el("nav", "album-tabs");
    tabs.setAttribute("aria-label", "Album views");
    const saved = link(
      "Photos",
      "#pet/" + pet.id,
      !view.candidates ? "active" : "",
    );
    const more = link(
      "More photos",
      "#pet/" + pet.id + "/more",
      view.candidates ? "active" : "",
    );
    tabs.append(saved, more);
    $("view").append(tabs);
  }
  const toolbar = el("div", "view-toolbar"),
    actions = el("div", "toolbar-actions");
  toolbar.append(
    el(
      "p",
      "",
      view.candidates
        ? `Select photos of ${pet.name}. Add them together.`
        : view.petId
          ? "Select photos to move them or remove a name."
          : "Open a photo to see its pets.",
    ),
  );
  if (view.petId)
    actions.append(button("Select shown", "text-button", selectAllVisible));
  actions.append(
    button("↻", "text-button", () => reloadPhotos(), "Refresh photos"),
  );
  if (!view.candidates && view.petId) {
    const source = el("select");
    source.setAttribute("aria-label", "Photo source");
    [
      ["all", "All photos"],
      ["automatic", "Automatic"],
      ["manual", "Named by you"],
    ].forEach(([value, text]) => {
      const option = el("option", "", text);
      option.value = value;
      source.append(option);
    });
    source.addEventListener("change", () => {
      view.source = source.value;
      reloadPhotos();
    });
    actions.prepend(source);
  }
  toolbar.append(actions);
  $("view").append(toolbar);
  addGrid();
}
function renderUnidentifiedShell() {
  heading("Unidentified", "Select photos. Give them a name together.");
  const toolbar = el("div", "view-toolbar"),
    chips = el("nav", "filter-chips");
  chips.setAttribute("aria-label", "Suggested pet");
  [["", "All photos"], ...pets.map((pet) => [pet.id, pet.name])].forEach(
    ([id, name]) =>
      chips.append(
        link(
          name,
          "#unidentified" + (id ? "/" + id : ""),
          "filter-chip" + (id === view.petId ? " active" : ""),
        ),
      ),
  );
  const actions = el("div", "toolbar-actions");
  actions.append(
    button("Select shown", "text-button", selectAllVisible),
    button("↻", "text-button", reloadPhotos, "Refresh photos"),
  );
  toolbar.append(chips, actions);
  $("view").append(toolbar);
  addGrid();
}
function addGrid() {
  const grid = el("div", "photo-grid");
  grid.id = "photo-grid";
  grid.setAttribute("aria-label", "Photos");
  for (let i = 0; i < 18; i++) grid.append(el("div", "skeleton"));
  const more = button("Show more photos", "secondary load-more", () =>
    loadItems(true),
  );
  more.id = "load-more";
  more.classList.add("hidden");
  $("view").append(grid, more);
}
async function loadItems(append = false, version = routeVersion) {
  if (view.loading) return;
  view.loading = true;
  const generation = boardGeneration;
  let retryRead = false;
  const current = view,
    groups = current.type === "unidentified" || current.candidates;
  const query = new URLSearchParams(
    groups
      ? { limit: 48, offset: append ? current.offset : 0 }
      : {
          status: "confirmed",
          tagging: "all",
          limit: 72,
          offset: append ? current.offset : 0,
          sort: "newest",
          source: current.source,
          unique_photos: true,
        },
  );
  if (current.petId) {
    query.set("pet_id", current.petId);
    if (groups) query.set("near_match", "true");
  }
  try {
    const data = await api((groups ? "groups?" : "detections?") + query);
    if (version !== routeVersion || current !== view) return;
    if (generation !== boardGeneration) {
      retryRead = true;
      return;
    }
    if (groups) {
      unidentifiedCount = data.total_photos;
      $("unidentified-count").textContent = number(unidentifiedCount);
    }
    const items = data.items
      .map((item) => ({ ...item, type: groups ? "group" : "photo" }))
      .filter((item) =>
        groups ? !removedGroups.has(item.id) : !removedPhotos.has(item.id),
      );
    const existing = new Set(append ? view.items.map(keyOf) : []);
    lastSelected = -1;
    view.items = append
      ? [...view.items, ...items.filter((item) => !existing.has(keyOf(item)))]
      : items;
    view.offset = (append ? view.offset : 0) + data.items.length;
    view.total = groups ? data.total_groups : data.total;
    renderGrid();
    renderSelection();
    $("load-more").classList.toggle("hidden", view.offset >= view.total);
  } catch (error) {
    if (version === routeVersion) {
      if (!view.items.length) {
        $("photo-grid").replaceChildren();
        empty($("photo-grid"), "Photos could not load", error.message);
      } else showError(error);
    }
  } finally {
    current.loading = false;
    if (retryRead && current === view && version === routeVersion)
      loadItems(append, version).catch(showError);
  }
}
function reloadPhotos() {
  if (!view || view.loading) return;
  selected.clear();
  view.offset = 0;
  return loadItems(false);
}
function renderGrid() {
  const grid = $("photo-grid");
  if (!grid) return;
  grid.classList.toggle(
    "selecting",
    Boolean(
      selectMode ||
        view.candidates ||
        view.type === "unidentified" ||
        selected.size,
    ),
  );
  grid.replaceChildren();
  view.items.forEach((item, index) => grid.append(photoTile(item, index)));
  if (!view.items.length)
    empty(
      grid,
      view.candidates
        ? "No more suggestions here"
        : view.type === "unidentified"
          ? "Nothing unidentified here"
          : "No photos yet",
      view.candidates
        ? "New matches will appear as your library grows."
        : view.type === "unidentified"
          ? "Your photos are already in pet albums."
          : "Photos will appear here as they are named.",
    );
}
function photoTile(item, index) {
  const key = keyOf(item),
    group = item.type === "group";
  const tile = el("div", "photo-tile" + (selected.has(key) ? " selected" : ""));
  tile.dataset.key = key;
  tile.tabIndex = 0;
  tile.setAttribute("role", group || selectMode ? "checkbox" : "button");
  tile.setAttribute(
    "aria-label",
    group
      ? `Select ${plural(item.photo_count, "photo")}${item.pet_name ? " suggested as " + item.pet_name : ""}`
      : `Open ${item.name}`,
  );
  if (group || selectMode)
    tile.setAttribute("aria-checked", String(selected.has(key)));
  const act = (event) => {
    if (group || selectMode || selected.size)
      toggleItem(item, index, event.shiftKey);
    else openViewer(view.items, index);
  };
  tile.addEventListener("click", act);
  tile.addEventListener("keydown", (event) => {
    if (event.target === tile && ["Enter", " "].includes(event.key)) {
      event.preventDefault();
      act(event);
    }
  });
  if (group) {
    const mosaic = el("div", "tile-mosaic");
    item.samples.forEach((row) => mosaic.append(image(row.id, row.name)));
    tile.append(mosaic);
    if (item.photo_count > 1)
      tile.append(el("span", "tile-count", "▣ " + number(item.photo_count)));
  } else tile.append(photoImage(item));
  tile.append(el("span", "photo-check", selected.has(key) ? "✓" : ""));
  if (group)
    tile.append(
      button(
        "⤢",
        "tile-zoom",
        () => openDetail(item),
        `Inspect ${plural(item.photo_count, "similar photo")}`,
      ),
    );
  return tile;
}
function toggleItem(item, index, range = false) {
  const value = !selected.has(keyOf(item));
  if (range && lastSelected >= 0) {
    for (
      let i = Math.min(index, lastSelected);
      i <= Math.min(view.items.length - 1, Math.max(index, lastSelected));
      i++
    ) {
      const key = keyOf(view.items[i]);
      if (value) selected.add(key);
      else selected.delete(key);
    }
  } else if (value) selected.add(keyOf(item));
  else selected.delete(keyOf(item));
  lastSelected = index;
  syncSelectionTiles();
  renderSelection();
}
function syncSelectionTiles() {
  const grid = $("photo-grid");
  if (!grid) return;
  grid.classList.toggle(
    "selecting",
    Boolean(
      selectMode ||
        view.candidates ||
        view.type === "unidentified" ||
        selected.size,
    ),
  );
  grid.querySelectorAll(".photo-tile").forEach((tile) => {
    const checked = selected.has(tile.dataset.key);
    tile.classList.toggle("selected", checked);
    tile.querySelector(".photo-check").textContent = checked ? "✓" : "";
    if (tile.getAttribute("role") === "checkbox")
      tile.setAttribute("aria-checked", String(checked));
  });
}
function selectAllVisible() {
  const all = view.items.every((item) => selected.has(keyOf(item)));
  if (all) selected.clear();
  else view.items.forEach((item) => selected.add(keyOf(item)));
  if (view.type === "album" && !view.candidates) {
    selectMode = true;
    const b = $("album-select");
    if (b) b.textContent = "Done";
    renderGrid();
  } else syncSelectionTiles();
  renderSelection();
}
function renderSelection() {
  const items = selectionItems(),
    bar = $("selection-bar");
  bar.replaceChildren();
  bar.classList.toggle("hidden", !items.length);
  document.body.classList.toggle("has-selection", Boolean(items.length));
  if (!items.length) return;
  const summary = el("div", "selection-summary");
  summary.append(
    el("strong", "", plural(photoIds(items).size, "photo") + " selected"),
    button("Clear", "", () => {
      selected.clear();
      syncSelectionTiles();
      renderSelection();
    }),
  );
  const actions = el("div", "selection-actions");
  if (view.candidates) {
    const pet = petById(view.petId);
    actions.append(
      button(`Add to ${pet.name}`, "primary", () => assignSelection(pet.id)),
    );
    pets
      .filter((other) => other.id !== pet.id)
      .forEach((other) =>
        actions.append(button(other.name, "", () => assignSelection(other.id))),
      );
    actions.append(
      button("Not my pets", "subtle", () => assignSelection(null, true)),
    );
  } else {
    pets.forEach((pet, index) => {
      const b = button(pet.name, "", () => assignSelection(pet.id));
      b.title =
        (items[0].type === "photo" ? "Move to " : "Name as ") +
        pet.name +
        " (" +
        (index + 1) +
        ")";
      b.append(el("span", "selection-shortcut", String(index + 1)));
      actions.append(b);
    });
    actions.append(
      button(
        items[0].type === "photo" ? "Remove name" : "Not my pets",
        "subtle",
        () => assignSelection(null, true),
      ),
    );
  }
  bar.append(summary, actions);
  renderUndo();
}
function removeLocally(op) {
  op.startedAt = performance.now();
  boardGeneration++;
  lastSelected = -1;
  op.routeKey = view?.key;
  op.positions = op.items.map((item) => ({
    item,
    index: view.items.findIndex((current) => keyOf(current) === keyOf(item)),
  }));
  op.items.forEach((item) => {
    (item.type === "group" ? removedGroups : removedPhotos).add(item.id);
    selected.delete(keyOf(item));
  });
  view.items = view.items.filter(
    (item) => !op.items.some((removed) => keyOf(item) === keyOf(removed)),
  );
  view.offset = Math.max(0, view.offset - op.items.length);
  renderGrid();
  renderSelection();
  op.uiMs = performance.now() - op.startedAt;
}
function restoreLocally(op, failedIds) {
  if (!op) {
    if (view && ["album", "unidentified"].includes(view.type)) reloadPhotos();
    return;
  }
  lastSelected = -1;
  const restore = op.positions.filter(
    ({ item }) => !failedIds || photoFailed(item, failedIds),
  );
  restore.forEach(({ item }) =>
    (item.type === "group" ? removedGroups : removedPhotos).delete(item.id),
  );
  if (view?.key !== op.routeKey) return;
  restore.forEach(({ item, index }) => {
    if (!view.items.some((current) => keyOf(current) === keyOf(item)))
      view.items.splice(
        Math.min(Math.max(0, index), view.items.length),
        0,
        item,
      );
  });
  renderGrid();
  renderSelection();
}
async function assignSelection(petId, skip = false) {
  const items = selectionItems();
  if (!items.length) return;
  if (
    items[0].type === "group" &&
    items.reduce((count, item) => count + item.crop_count, 0) > 5000
  ) {
    toast("Choose up to 5,000 photos at a time. Your selection is still here.");
    return;
  }
  const groups = items[0].type === "group",
    name = petById(petId)?.name;
  const body = groups
    ? { group_ids: items.map((item) => item.id), pet_id: petId, skip }
    : {
        items: items.flatMap((item) =>
          assignmentsFor(item).map((id) => ({
            detection_id: id,
            pet_id: petId,
            status: skip ? "pending" : "confirmed",
            expected_pet_id: item.pet_id,
            expected_status: item.status,
          })),
        ),
      };
  const op = {
    kind: groups ? "groups" : "photos",
    path: groups ? "groups/batch" : "review/batch",
    body,
    items,
    label: skip
      ? groups
        ? "Photos hidden"
        : "Names removed"
      : `Added to ${name}`,
    petId,
    skip,
    count: photoIds(items).size,
  };
  op.body.operation_id = operationId();
  removeLocally(op);
  outbox.enqueue(op);
}
const outbox = new window.PetActionQueue({
  send: async (op) => {
    let result;
    if (op.body.items && op.body.items.length > 100) {
      const results = [];
      for (let offset = 0; offset < op.body.items.length; offset += 100) {
        try {
          const part = await api(op.path, {
            method: "POST",
            body: JSON.stringify({
              items: op.body.items.slice(offset, offset + 100),
              operation_id:
                (op.body.operation_id ||
                  (op.body.operation_id = operationId())) +
                "-" +
                offset,
            }),
          });
          results.push(...part.results);
        } catch (error) {
          error.failedIds = new Set([
            ...results.filter((row) => !row.saved).map((row) => row.id),
            ...op.body.items.slice(offset).map((item) => item.detection_id),
          ]);
          throw error;
        }
      }
      result = { results, failed: results.filter((row) => !row.saved).length };
    } else
      result = await api(op.path, {
        method: "POST",
        body: JSON.stringify(op.body),
      });
    if (result.failed) {
      const error = new Error(
        `${result.failed} photos could not be changed. Your other changes were saved.`,
      );
      error.failedIds = new Set(
        result.results.filter((row) => !row.saved).map((row) => row.id),
      );
      throw error;
    }
    return result;
  },
  onChange: () => {
    renderActivity();
  },
  onSuccess: (op, result) => {
    if (op.startedAt)
      console.info(
        "Pet action saved " +
          JSON.stringify({
            uiMs: Math.round(op.uiMs || 0),
            saveMs: Math.round(performance.now() - op.startedAt),
            photos: result.photos ?? op.count,
          }),
      );
    boardGeneration++;
    op.items.forEach((item) =>
      (item.type === "group" ? removedGroups : removedPhotos).delete(item.id),
    );
    if (op.kind === "photos") {
      dismissedUndoId = status.undo_group?.id || dismissedUndoId;
      sessionStorage.setItem("pet-dismissed-undo", dismissedUndoId);
    }
    if (op.kind === "undo") {
      lastUndo = null;
      status.undo_group = null;
      restoreLocally(op.original);
      toast("Change undone");
    } else {
      if (op.kind === "groups") {
        if (result.undo_id)
          lastUndo = {
            kind: "group",
            id: result.undo_id,
            label: op.label,
            original: op,
          };
      } else lastUndo = { kind: "photos", label: op.label, original: op };
      toast(`${op.count ? plural(op.count, "photo") + " · " : ""}${op.label}`);
    }
    renderUndo();
    refreshAlbums().catch(() => {});
    // A new page can load immediately; no policy fit, status request, or full
    // library refresh belongs to this action's completion path.
    if (view?.key === op.routeKey && !view.items.length && !view.loading)
      loadItems(false).catch(showError);
  },
  onError: (op, error) => {
    boardGeneration++;
    if (error.failedIds)
      op.items
        .filter((item) => !photoFailed(item, error.failedIds))
        .forEach((item) =>
          (item.type === "group" ? removedGroups : removedPhotos).delete(
            item.id,
          ),
        );
    if (op.kind !== "undo") {
      restoreLocally(op, error.failedIds);
      if (error.failedIds) {
        op.items = op.items.filter((item) =>
          photoFailed(item, error.failedIds),
        );
        op.positions = op.positions.filter(({ item }) =>
          photoFailed(item, error.failedIds),
        );
        if (op.body.items)
          op.body.items = op.body.items.filter((item) =>
            error.failedIds.has(item.detection_id),
          );
        op.count = photoIds(op.items).size;
        op.body.operation_id = operationId();
      }
    }
    showError(error);
    renderUndo();
  },
});
function retryFailed(failure) {
  const op = failure.op;
  if (op.kind !== "undo") {
    if (view?.key === op.routeKey) removeLocally(op);
    else
      op.items.forEach((item) =>
        (item.type === "group" ? removedGroups : removedPhotos).add(item.id),
      );
  }
  outbox.retry(failure.id);
}
function renderUndo() {
  const bar = $("undo-bar"),
    failed = outbox.failed.at(-1);
  bar.classList.toggle("hidden", !lastUndo && !failed);
  if (!lastUndo && !failed) return;
  $("undo-label").textContent = failed
    ? "A change wasn’t saved"
    : lastUndo.label;
  $("undo-action").textContent = failed ? "Retry" : "Undo";
  $("undo-action").disabled = Boolean(outbox.pending);
}
$("undo-action").addEventListener("click", () => {
  const failed = outbox.failed.at(-1);
  if (failed) {
    retryFailed(failed);
    return;
  }
  if (!lastUndo || outbox.pending) return;
  const original = lastUndo.original;
  if (lastUndo.kind === "group")
    outbox.enqueue({
      kind: "undo",
      path: "groups/undo/" + lastUndo.id,
      body: { operation_id: operationId() },
      original,
      items: [],
      label: "Undo",
    });
  else {
    const body = {
      items: original.items.flatMap((item) =>
        assignmentsFor(item).map((id) => ({
          detection_id: id,
          pet_id: item.pet_id,
          status: item.status,
          expected_pet_id: original.petId || undefined,
          expected_status: original.skip ? "pending" : "confirmed",
        })),
      ),
    };
    outbox.enqueue({
      kind: "undo",
      path: "review/batch",
      body: { ...body, operation_id: operationId() },
      original,
      items: [],
      label: "Undo",
    });
  }
});
$("dismiss-undo").addEventListener("click", () => {
  if (outbox.failed.length) {
    navigate("activity");
    return;
  }
  dismissedUndoId = lastUndo?.id || status.undo_group?.id || "";
  sessionStorage.setItem("pet-dismissed-undo", dismissedUndoId);
  lastUndo = null;
  status.undo_group = null;
  renderUndo();
});
let viewerItems = [];
function openViewer(items, index) {
  viewerItems = items;
  viewerIndex = index;
  renderViewer();
  if (!$("viewer").open) $("viewer").showModal();
}
function renderViewer() {
  const row = viewerItems[viewerIndex];
  if (!row) return;
  $("viewer-name").textContent = row.name || "Photo";
  const img = $("viewer-image");
  const preview = cropCache.get(`photo:${row.id}:320`);
  if (preview) img.src = preview;
  else img.removeAttribute("src");
  img.alt = row.name || "Pet photo";
  scheduleCrop(img, row.id, 1280, "photo");
  $("viewer-previous").disabled = viewerIndex === 0;
  $("viewer-next").disabled = viewerIndex === viewerItems.length - 1;
  const actions = $("viewer-actions");
  actions.replaceChildren();
  actions.append(
    el(
      "span",
      "",
      row.identities?.map((identity) => identity.name).join(" · ") ||
        row.pet_name ||
        "Unidentified",
    ),
  );
  if (view?.type === "album" && !view.petId) {
    (row.identities || []).forEach((identity) =>
      actions.append(
        button("Open " + identity.name, "", () => {
          $("viewer").close();
          navigate("pet/" + identity.pet_id);
        }),
      ),
    );
    return;
  }
  pets.forEach((pet) =>
    actions.append(
      button(pet.name, row.pet_id === pet.id ? "active" : "", () =>
        changeViewerPhoto(row, pet.id),
      ),
    ),
  );
  if (row.status === "confirmed") {
    actions.append(
      button("Remove name", "quiet", () => changeViewerPhoto(row, null)),
    );
    actions.append(
      button("Use as album cover", "quiet", async () => {
        const pet = petById(row.pet_id),
          previous = pet.cover_id;
        pet.cover_id = row.id;
        renderNavigation();
        toast("Album cover updated");
        try {
          await api(`pets/${row.pet_id}/cover`, {
            method: "PUT",
            body: JSON.stringify({ detection_id: row.id }),
          });
        } catch (error) {
          pet.cover_id = previous;
          renderNavigation();
          throw error;
        }
      }),
    );
  }
}
function changeViewerPhoto(row, petId) {
  const op = {
    kind: "photos",
    path: "review/batch",
    body: {
      items: assignmentsFor(row).map((id) => ({
        detection_id: id,
        pet_id: petId,
        status: petId ? "confirmed" : "pending",
        expected_pet_id: row.pet_id,
        expected_status: row.status,
      })),
    },
    items: [{ ...row, type: "photo" }],
    positions: [
      {
        item: { ...row, type: "photo" },
        index: view?.items.findIndex((item) => item.id === row.id) ?? 0,
      },
    ],
    routeKey: view?.key,
    label: petId ? "Moved to " + petById(petId).name : "Name removed",
    skip: !petId,
    petId,
    count: 1,
  };
  if (view?.items.some((item) => item.id === row.id)) removeLocally(op);
  op.body.operation_id = operationId();
  $("viewer").close();
  outbox.enqueue(op);
}
$("viewer-close").addEventListener("click", () => $("viewer").close());
$("viewer-previous").addEventListener("click", () => {
  if (viewerIndex > 0) {
    viewerIndex--;
    renderViewer();
  }
});
$("viewer-next").addEventListener("click", () => {
  if (viewerIndex < viewerItems.length - 1) {
    viewerIndex++;
    renderViewer();
  }
});
async function openDetail(group) {
  const data = await api("groups/" + group.id);
  detailGroup = { ...data, type: "group" };
  detailSelected = new Set(data.members.map((row) => row.id));
  $("detail-title").textContent = plural(data.photo_count, "similar photo");
  $("detail-grid").replaceChildren();
  data.members.forEach((row) => {
    const tile = button(
      "",
      "photo-tile selected",
      () => {
        if (detailSelected.has(row.id)) detailSelected.delete(row.id);
        else detailSelected.add(row.id);
        renderDetailSelection();
      },
      `Include ${row.name}`,
    );
    tile.dataset.id = row.id;
    tile.setAttribute("role", "checkbox");
    tile.setAttribute("aria-checked", "true");
    tile.append(image(row.id, row.name), el("span", "photo-check", "✓"));
    $("detail-grid").append(tile);
  });
  $("detail-grid").classList.add("selecting");
  renderDetailSelection();
  $("group-detail").showModal();
}
function renderDetailSelection() {
  $("detail-count").textContent =
    plural(
      new Set(
        detailGroup.members
          .filter((row) => detailSelected.has(row.id))
          .map((row) => row.asset_id),
      ).size,
      "photo",
    ) + " selected";
  $("detail-toggle").textContent =
    detailSelected.size === detailGroup.members.length
      ? "Clear selection"
      : "Select all";
  $("detail-grid")
    .querySelectorAll(".photo-tile")
    .forEach((tile) => {
      const checked = detailSelected.has(tile.dataset.id);
      tile.classList.toggle("selected", checked);
      tile.setAttribute("aria-checked", String(checked));
      tile.querySelector(".photo-check").textContent = checked ? "✓" : "";
    });
  const actions = $("detail-actions");
  actions.replaceChildren();
  pets.forEach((pet) => {
    const b = button(
      "Add to " + pet.name,
      pet.id === view?.petId ? "primary" : "",
      () => submitDetail(pet.id),
    );
    b.disabled = !detailSelected.size;
    actions.append(b);
  });
}
function submitDetail(petId) {
  const group = detailGroup,
    excluded = group.members
      .filter((row) => !detailSelected.has(row.id))
      .map((row) => row.id);
  const item = view.items.find((row) => row.id === group.id) || group;
  const op = {
    kind: "groups",
    path: "groups/" + group.id + "/decision",
    body: { pet_id: petId, excluded, skip: false },
    items: [item],
    label: "Added to " + petById(petId).name,
    petId,
    count: new Set(
      group.members
        .filter((row) => detailSelected.has(row.id))
        .map((row) => row.asset_id),
    ).size,
  };
  removeLocally(op);
  op.body.operation_id = operationId();
  $("group-detail").close();
  outbox.enqueue(op);
}
$("detail-close").addEventListener("click", () => $("group-detail").close());
$("detail-toggle").addEventListener("click", () => {
  detailSelected =
    detailSelected.size === detailGroup.members.length
      ? new Set()
      : new Set(detailGroup.members.map((row) => row.id));
  renderDetailSelection();
});
function renderSettings() {
  $("view").replaceChildren();
  heading("Settings");
  const container = el("div", "settings-container");
  $("view").append(container);
  const sorting = el("section", "settings-section");
  sorting.append(el("h2", "", "Automatic sorting"));
  const row = el("div", "setting-row"),
    copy = el("div");
  copy.append(
    el("strong", "", "Sorting preference"),
    el("p", "", "Choose the balance between coverage and mistakes."),
  );
  const profile = el("select");
  profile.setAttribute("aria-label", "Sorting preference");
  [
    ["careful", "Fewer mistakes"],
    ["balanced", "Balanced"],
    ["broader", "Sort more"],
  ].forEach(([value, text]) => {
    const o = el("option", "", text);
    o.value = value;
    profile.append(o);
  });
  profile.value = status.matching?.profile || "balanced";
  profile.addEventListener("change", async () => {
    const value = profile.value;
    profile.disabled = true;
    try {
      await api("matching", {
        method: "PUT",
        body: JSON.stringify({ mode: "adaptive", profile: value }),
      });
      toast("Sorting preference saved");
    } catch (error) {
      showError(error);
    } finally {
      profile.disabled = false;
      refreshStatus().catch(() => {});
    }
  });
  row.append(copy, profile);
  sorting.append(row);
  pets.forEach((pet) => {
    const r = el("div", "setting-row"),
      label = el("div");
    label.append(
      image(pet.cover_id, "", "nav-avatar", 160),
      el("strong", "", pet.name),
    );
    const toggle = button("", "switch", async () => {
      const enabled = toggle.getAttribute("aria-checked") !== "true";
      toggle.setAttribute("aria-checked", String(enabled));
      try {
        await api("pets/" + pet.id + "/automation", {
          method: "PUT",
          body: JSON.stringify({ enabled }),
        });
        pet.auto_tag = enabled;
      } catch (error) {
        toggle.setAttribute("aria-checked", String(!enabled));
        showError(error);
      }
    });
    toggle.setAttribute("role", "switch");
    toggle.setAttribute("aria-label", "Automatic sorting for " + pet.name);
    toggle.setAttribute("aria-checked", String(pet.auto_tag));
    r.append(label, toggle);
    sorting.append(r);
  });
  container.append(sorting);
  const add = el("section", "settings-section");
  add.append(el("h2", "", "Add a pet"));
  const form = el("form", "settings-form"),
    label = el("label", "", "Name"),
    name = el("input");
  name.required = true;
  name.maxLength = 64;
  name.placeholder = "Pet’s name";
  name.autocomplete = "off";
  label.append(name);
  const speciesLabel = el("label", "", "Species"),
    species = el("select");
  [
    ["dog", "Dog"],
    ["cat", "Cat"],
  ].forEach(([value, text]) => {
    const o = el("option", "", text);
    o.value = value;
    species.append(o);
  });
  speciesLabel.append(species);
  const submit = el("button", "primary", "Add pet");
  submit.type = "submit";
  form.append(label, speciesLabel, submit);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
      await api("pets", {
        method: "POST",
        body: JSON.stringify({ name: name.value, species: species.value }),
      });
      await refreshStatus();
      toast("Pet added");
      renderSettings();
    } catch (error) {
      showError(error);
    } finally {
      submit.disabled = false;
    }
  });
  add.append(form);
  container.append(add);
  const activity = el("section", "settings-section");
  activity.append(
    el("h2", "", "Library"),
    link("Background activity →", "#activity", "text-button"),
  );
  const note = el(
    "p",
    "settings-note",
    "New photos are checked automatically. Naming and correcting photos helps future sorting.",
  );
  activity.append(note);
  container.append(activity);
  const connection = el("section", "settings-section");
  connection.append(el("h2", "", "Connection"));
  const dl = el("dl", "connection-grid");
  [
    [
      "Immich",
      status.immich_connected
        ? "Connected"
        : status.demo
          ? "Demo mode"
          : "Not connected",
    ],
    ["Recognition", status.provider || "—"],
    ["Detection model", status.detector || "—"],
    ["Identity model", status.recognizer || "—"],
  ].forEach(([key, value]) =>
    dl.append(el("dt", "", key), el("dd", "", value)),
  );
  connection.append(dl);
  if (status.auth_required)
    connection.append(
      button("Forget service token", "text-button", () => {
        token = "";
        sessionStorage.removeItem("pet-service-token");
        refreshStatus().catch(showError);
      }),
    );
  container.append(connection);
}
function renderActivityPage() {
  if (view?.type !== "activity") return;
  $("view").replaceChildren();
  heading(
    "Background activity",
    "You can keep browsing and naming photos while this runs.",
  );
  const container = el("div", "settings-container");
  $("view").append(container);
  const check = status.recheck || {},
    section = el("section", "settings-section"),
    line = el("div", "activity-detail");
  const active = ["queued", "preparing", "running"].includes(check.state);
  line.append(
    el(
      "strong",
      "",
      active
        ? "Organizing your library"
        : check.state === "failed"
          ? "Sorting needs attention"
          : "Library up to date",
    ),
    el("span", "", active && check.percent != null ? check.percent + "%" : ""),
  );
  section.append(line);
  if (active) {
    const progress = el("progress", "activity-progress");
    progress.max = 100;
    if (check.percent != null) progress.value = check.percent;
    section.append(progress);
  }
  section.append(
    el(
      "p",
      "settings-note",
      active
        ? `${number(check.completed)} of ${number(check.total)} checked.`
        : check.state === "failed"
          ? check.error
          : "Clear matches are added automatically. There is nothing you need to wait for here.",
    ),
  );
  if (status.review_summary?.pending_tag_changes)
    section.append(
      el(
        "p",
        "settings-note",
        plural(status.review_summary.pending_tag_changes, "tag change") +
          " syncing to Immich.",
      ),
    );
  if (outbox.pending)
    section.append(
      el("p", "settings-note", plural(outbox.pending, "change") + " saving…"),
    );
  outbox.failed.forEach((failure) => {
    const r = el("div", "setting-row");
    r.append(
      el("p", "error-text", failure.error.message),
      button("Retry", "secondary", () => retryFailed(failure)),
      button("Dismiss", "text-button", () => {
        outbox.discard(failure.id);
        renderActivityPage();
      }),
    );
    section.append(r);
  });
  const controls = el("div", "heading-actions");
  controls.style.marginTop = "20px";
  [
    ["Check for new photos", "sync"],
    ["Scan existing library", "sync?full=true"],
    ["Recheck names", "rematch"],
    ["Retry failures", "retry"],
  ].forEach(([text, path]) =>
    controls.append(
      button(text, "secondary small-button", async () => {
        await api(path, { method: "POST" });
        toast("Background work queued");
        refreshStatus().catch(() => {});
      }),
    ),
  );
  controls.style.flexWrap = "wrap";
  section.append(controls);
  container.append(section);
  const jobsSection = el("section", "settings-section");
  jobsSection.append(el("h2", "", "Recent processing"));
  const list = el("div", "job-list");
  jobsSection.append(list);
  container.append(jobsSection);
  const version = routeVersion;
  api("jobs")
    .then((jobs) => {
      if (version !== routeVersion || view.type !== "activity") return;
      jobs.forEach((job) => {
        const row = el("div", "job-row"),
          name = el("div", "", job.name);
        if (job.error) name.append(el("small", "", job.error));
        row.append(name, el("span", "job-state " + job.status, job.status));
        list.append(row);
      });
    })
    .catch(() => {});
}
$("activity-indicator").addEventListener("click", () => navigate("activity"));
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  token = $("service-token").value;
  try {
    await refreshStatus();
    sessionStorage.setItem("pet-service-token", token);
    $("service-token").value = "";
    $("login-dialog").close();
    await openRoute();
  } catch (error) {
    $("login-error").textContent = error.message;
  }
});
$("login-dialog").addEventListener("cancel", (event) => event.preventDefault());
window.addEventListener("hashchange", () => openRoute().catch(showError));
window.addEventListener("beforeunload", (event) => {
  if (outbox.pending) {
    event.preventDefault();
    event.returnValue = "";
  }
});
document.addEventListener("keydown", (event) => {
  if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName))
    return;
  if ($("viewer").open) {
    if (event.key === "ArrowLeft") $("viewer-previous").click();
    if (event.key === "ArrowRight") $("viewer-next").click();
    return;
  }
  if ($("group-detail").open || $("login-dialog").open) return;
  if (event.key === "Escape") {
    selected.clear();
    syncSelectionTiles();
    renderSelection();
  }
  if (/^[1-9]$/.test(event.key) && selected.size) {
    const pet = pets[Number(event.key) - 1];
    if (pet) {
      event.preventDefault();
      assignSelection(pet.id);
    }
  }
});
(async () => {
  try {
    pets = await api("collections");
    renderNavigation();
    await openRoute();
    refreshStatus().catch(() => {});
  } catch (error) {
    if (!$("login-dialog").open) showError(error);
  }
})();
setInterval(() => {
  if (!document.hidden && !$("login-dialog").open)
    refreshStatus().catch(() => {});
}, 5000);
