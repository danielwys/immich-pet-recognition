const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function session() {
  const nodes = new Map(),
    storage = new Map(),
    responses = new Map(),
    errors = [];
  function element(tag = "div") {
    const classes = new Set(),
      attributes = new Map(),
      listeners = new Map();
    let id,
      text = "";
    return {
      tagName: tag.toUpperCase(),
      children: [],
      get id() {
        return id;
      },
      set id(value) {
        id = value;
        nodes.set(value, this);
      },
      get textContent() {
        return text + this.children.map((child) => child.textContent).join("");
      },
      set textContent(value) {
        text = String(value);
        this.children = [];
      },
      classList: {
        toggle(name, value) {
          if (value) classes.add(name);
          else classes.delete(name);
        },
        add(name) {
          classes.add(name);
        },
        remove(name) {
          classes.delete(name);
        },
        contains(name) {
          return classes.has(name);
        },
      },
      addEventListener(type, listener) {
        listeners.set(type, listener);
      },
      click() {
        return listeners.get("click")?.({ target: this, stopPropagation() {} });
      },
      showModal() {
        this.open = true;
      },
      close() {
        this.open = false;
      },
      replaceChildren(...children) {
        text = "";
        this.children = children;
      },
      append(...children) {
        this.children.push(...children);
      },
      querySelectorAll(selector) {
        const descendants = this.children.flatMap((child) => [
          child,
          ...child.querySelectorAll("*"),
        ]);
        return selector === "*"
          ? descendants
          : descendants.filter((child) =>
              child.className?.split(" ").includes(selector.slice(1)),
            );
      },
      querySelector(selector) {
        return this.querySelectorAll(selector)[0] || null;
      },
      setAttribute(key, value) {
        attributes.set(key, value);
      },
      getAttribute(key) {
        return attributes.get(key) || null;
      },
      removeAttribute(key) {
        attributes.delete(key);
      },
      dataset: {},
      open: false,
    };
  }
  let phase = "bootstrap",
    releaseStatus;
  const response = (body) => ({
    ok: true,
    status: 200,
    json: async () => body,
  });
  const document = {
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, element());
      return nodes.get(id);
    },
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
    createElement: element,
    createElementNS: (_, tag) => element(tag),
    addEventListener() {},
    body: element(),
    images: [],
    activeElement: { tagName: "BODY" },
  };
  const window = {
    addEventListener() {},
    reportError(error) {
      errors.push(error);
    },
  };
  const context = vm.createContext({
    window,
    document,
    sessionStorage: {
      getItem: (key) => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key),
    },
    location: { hash: "#settings" },
    console,
    performance: { now: () => 0 },
    IntersectionObserver: class {
      observe() {}
      unobserve() {}
    },
    setInterval() {},
    setTimeout() {},
    clearTimeout() {},
    fetch(url) {
      if (phase === "bootstrap") return new Promise(() => {});
      if (responses.has(url))
        return Promise.resolve(response(responses.get(url)));
      if (url === "/api/status")
        return new Promise((resolve) => {
          releaseStatus = (body) =>
            resolve(
              response(
                body || {
                  undo_group: {
                    id: "completed-group",
                    pet_id: "pet",
                    skip: false,
                  },
                  recheck: { state: "complete" },
                  review_summary: { pending_tag_changes: 0 },
                  jobs: {},
                },
              ),
            );
        });
      return Promise.resolve(
        response(
          url.startsWith("/api/groups/undo/")
            ? { restored: 1 }
            : url.startsWith("/api/groups?")
              ? { total_photos: 0 }
              : [],
        ),
      );
    },
  });
  const source = path.resolve(
    __dirname,
    "../../src/immich_pet_recognition/static",
  );
  vm.runInContext(
    fs.readFileSync(path.join(source, "action-queue.js"), "utf8"),
    context,
  );
  vm.runInContext(
    fs.readFileSync(path.join(source, "app.js"), "utf8"),
    context,
  );
  return {
    context,
    nodes,
    storage,
    errors,
    respond(path, body) {
      responses.set(path, body);
    },
    start() {
      phase = "responses";
    },
    release(body) {
      releaseStatus(body);
    },
  };
}

test("a status response started before Undo cannot bring that completed action back", async () => {
  const app = session();
  app.start();
  vm.runInContext(
    "view={type:'settings',key:'settings',items:[]}; status={undo_group:{id:'completed-group'}}; lastUndo={kind:'group',id:'completed-group',label:'Added to pet'};",
    app.context,
  );
  const pendingStatus = vm.runInContext("refreshStatus()", app.context);
  vm.runInContext(
    "outbox.enqueue({kind:'undo',path:'groups/undo/completed-group',body:{},items:[]});",
    app.context,
  );
  for (
    let tick = 0;
    tick < 20 && vm.runInContext("outbox.pending", app.context);
    tick++
  )
    await new Promise(setImmediate);
  assert.equal(vm.runInContext("outbox.pending", app.context), 0);
  assert.equal(vm.runInContext("lastUndo", app.context), null);
  app.release();
  await pendingStatus;
  assert.equal(vm.runInContext("lastUndo", app.context), null);
  assert.equal(app.storage.get("pet-dismissed-undo"), "completed-group");
  assert.equal(app.nodes.get("undo-bar").classList.contains("hidden"), true);
  assert.deepEqual(app.errors, []);
});

test("connection details load and keep updating without clearing a Settings form", async () => {
  const app = session();
  app.start();
  vm.runInContext("view={type:'settings'}; renderSettings();", app.context);
  const details = app.nodes.get("connection-details");
  const values = () =>
    details.children
      .filter((child) => child.tagName === "DD")
      .map((child) => child.textContent);
  assert.deepEqual(values(), [
    "Checking…",
    "Checking…",
    "Checking…",
    "Checking…",
  ]);
  function descendants(node) {
    return [node, ...node.children.flatMap(descendants)];
  }
  const form = descendants(app.nodes.get("view")).find(
    (node) => node.tagName === "FORM",
  );
  const name = descendants(form).find((node) => node.tagName === "INPUT");
  name.value = "Still typing this name";
  const healthy = {
    immich_connected: true,
    provider: "CUDAExecutionProvider",
    detector: "open-noodle/rfdetr-nano",
    recognizer: "open-noodle/pet-recognition-large",
  };
  let pending = vm.runInContext("refreshStatus()", app.context);
  app.release(healthy);
  await pending;
  assert.deepEqual(values(), [
    "Connected",
    healthy.provider,
    healthy.detector,
    healthy.recognizer,
  ]);
  assert.equal(descendants(app.nodes.get("view")).includes(form), true);
  assert.equal(name.value, "Still typing this name");

  pending = vm.runInContext("refreshStatus()", app.context);
  app.release({ ...healthy, immich_connected: false });
  await pending;
  assert.equal(values()[0], "Not connected");
  assert.equal(descendants(app.nodes.get("view")).includes(form), true);
  assert.equal(name.value, "Still typing this name");

  pending = vm.runInContext("refreshStatus()", app.context);
  app.release(healthy);
  await pending;
  assert.equal(values()[0], "Connected");
  assert.deepEqual(app.errors, []);
});

test("expanded groups show whole photos once and preserve selection when opening them larger", async () => {
  const app = session();
  app.start();
  app.respond("/api/groups/inspect", {
    id: "inspect",
    photo_count: 2,
    members: [
      { id: "crop-a", asset_id: "photo-one", name: "one.jpg" },
      { id: "crop-b", asset_id: "photo-one", name: "one.jpg" },
      { id: "crop-c", asset_id: "photo-two", name: "two.jpg" },
    ],
  });
  vm.runInContext(
    "view={type:'unidentified',items:[]}; pets=[{id:'mimi',name:'Mimi'}]; cropCache.set('photo:crop-a:1280','full-one'); cropCache.set('photo:crop-c:1280','full-two');",
    app.context,
  );
  await vm.runInContext("openDetail({id:'inspect'})", app.context);
  const grid = app.nodes.get("detail-grid");
  assert.equal(grid.children.length, 2);
  for (const tile of grid.children) {
    assert.equal(tile.children[0].dataset.mediaKind, "photo");
    assert.equal(tile.children[0].dataset.cropSize, 640);
  }
  grid.children[0].querySelector(".tile-zoom").click();
  assert.equal(app.nodes.get("viewer").open, true);
  assert.equal(app.nodes.get("group-detail").open, true);
  assert.equal(app.nodes.get("viewer-image").src, "full-one");
  assert.equal(vm.runInContext("detailSelected.size", app.context), 3);
  const actions = app.nodes.get("viewer-actions");
  assert.deepEqual(
    actions.children
      .filter((e) => e.tagName === "BUTTON")
      .map((e) => e.textContent),
    ["Included in group", "Back to group"],
  );
  actions.children.find((e) => e.textContent === "Included in group").click();
  assert.deepEqual(
    JSON.parse(
      vm.runInContext("JSON.stringify([...detailSelected])", app.context),
    ),
    ["crop-c"],
  );
  assert.equal(app.nodes.get("detail-count").textContent, "1 photo selected");
  assert.equal(grid.children[0].getAttribute("aria-checked"), "false");
  actions.children.find((e) => e.textContent === "Back to group").click();
  assert.equal(app.nodes.get("viewer").open, false);
  assert.equal(app.nodes.get("group-detail").open, true);
  grid.children[0].click();
  assert.equal(app.nodes.get("detail-count").textContent, "2 photos selected");
  assert.equal(vm.runInContext("detailSelected.size", app.context), 3);
  app.nodes.get("detail-toggle").click();
  assert.equal(app.nodes.get("detail-count").textContent, "0 photos selected");
  assert.equal(vm.runInContext("outbox.pending", app.context), 0);
  assert.deepEqual(app.errors, []);
});
