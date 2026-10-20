const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function session() {
  const nodes = new Map(),
    storage = new Map(),
    errors = [];
  function element() {
    const classes = new Set();
    return {
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
      addEventListener() {},
      replaceChildren() {},
      append() {},
      querySelectorAll() {
        return [];
      },
      setAttribute() {},
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
      if (url === "/api/status")
        return new Promise((resolve) => {
          releaseStatus = () =>
            resolve(
              response({
                undo_group: {
                  id: "completed-group",
                  pet_id: "pet",
                  skip: false,
                },
                recheck: { state: "complete" },
                review_summary: { pending_tag_changes: 0 },
                jobs: {},
              }),
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
    start() {
      phase = "responses";
    },
    release() {
      releaseStatus();
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
