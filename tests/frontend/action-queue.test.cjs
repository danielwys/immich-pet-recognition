const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(
    __dirname,
    "../../src/immich_pet_recognition/static/action-queue.js",
  ),
  "utf8",
);

function loadQueue() {
  const errors = [];
  const window = { reportError: (error) => errors.push(error) };
  vm.runInNewContext(source, { window });
  return { Queue: window.PetActionQueue, errors };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

const settled = () => new Promise(setImmediate);

test("consecutive enqueue calls return immediately and send in FIFO order", async () => {
  const { Queue } = loadQueue();
  const first = deferred();
  const sent = [];
  const successes = [];
  const states = [];
  const one = Object.freeze({ photos: ["one"], pet: "Butter" });
  const two = Object.freeze({ photos: ["two"], pet: "Rusty" });
  const queue = new Queue({
    send: (op) => {
      sent.push(op);
      return op === one ? first.promise : "second saved";
    },
    onSuccess: (op, result) => successes.push([op, result]),
    onChange: (current) => states.push([current.pending, current.running]),
  });

  const oneId = queue.enqueue(one);
  const twoId = queue.enqueue(two);
  assert.notEqual(oneId, twoId);
  assert.equal(queue.pending, 2);
  assert.equal(queue.running, false);
  assert.equal(sent.length, 0, "send must not block the enqueue call");
  assert.deepEqual(states, [
    [1, false],
    [2, false],
  ]);

  await settled();
  assert.deepEqual(sent, [one]);
  assert.equal(queue.pending, 2, "pending includes the in-flight request");
  assert.equal(queue.running, true);

  first.resolve("first saved");
  await settled();
  assert.deepEqual(sent, [one, two]);
  assert.deepEqual(successes, [
    [one, "first saved"],
    [two, "second saved"],
  ]);
  assert.equal(queue.pending, 0);
  assert.equal(queue.running, false);
  assert.equal(queue.failed.length, 0);
  assert.deepEqual(states.at(-1), [0, false]);
});

test("a failed request does not stall later operations and can be retried once", async () => {
  const { Queue } = loadQueue();
  const failure = new Error("Connection lost");
  const attempts = [];
  const failures = [];
  const retried = deferred();
  const bad = { photos: ["one"], pet: "Butter" };
  const good = { photos: ["two"], pet: "Rusty" };
  let shouldFail = true;
  const queue = new Queue({
    send: (op) => {
      attempts.push(op);
      if (op === bad && shouldFail) return Promise.reject(failure);
      return op === bad ? retried.promise : "saved";
    },
    onError: (op, error) => failures.push([op, error]),
  });
  const id = queue.enqueue(bad);
  queue.enqueue(good);
  await settled();

  assert.deepEqual(attempts, [bad, good]);
  assert.deepEqual(failures, [[bad, failure]]);
  assert.equal(queue.pending, 0);
  assert.equal(queue.failed.length, 1);
  assert.equal(queue.failed[0].id, id);
  assert.equal(queue.failed[0].op, bad);
  assert.equal(queue.failed[0].error, failure);

  shouldFail = false;
  assert.equal(queue.retry(id), true);
  assert.equal(queue.pending, 1);
  assert.equal(queue.failed.length, 0);
  assert.equal(
    queue.retry(id),
    false,
    "duplicate queued retries must be ignored",
  );
  await settled();
  assert.equal(
    queue.retry(id),
    false,
    "duplicate in-flight retries must be ignored",
  );
  assert.deepEqual(attempts, [bad, good, bad]);

  retried.resolve("saved on retry");
  await settled();
  assert.equal(queue.pending, 0);
  assert.equal(queue.failed.length, 0);
  assert.equal(
    queue.retry(id),
    false,
    "completed operations cannot be resent via retry",
  );
});

test("a synchronous send error is recorded and does not prevent the next send", async () => {
  const { Queue } = loadQueue();
  const failure = new Error("Offline");
  const sent = [];
  const queue = new Queue({
    send: (op) => {
      sent.push(op);
      if (op === "first") throw failure;
      return "saved";
    },
  });
  queue.enqueue("first");
  queue.enqueue("second");
  await settled();
  assert.deepEqual(sent, ["first", "second"]);
  assert.equal(queue.failed[0].error, failure);
  assert.equal(queue.running, false);
});

test("discard clears a failed action and cannot discard queued or in-flight changes", async () => {
  const { Queue } = loadQueue();
  const first = deferred();
  const states = [];
  const queue = new Queue({
    send: (op) => (op === "first" ? first.promise : "saved"),
    onChange: (current) =>
      states.push([current.pending, current.failed.length]),
  });
  const firstId = queue.enqueue("first");
  const secondId = queue.enqueue("second");
  assert.equal(queue.discard(firstId), false);
  assert.equal(queue.discard(secondId), false);
  assert.equal(queue.pending, 2);
  await settled();
  assert.equal(queue.discard(firstId), false);
  first.reject(new Error("Offline"));
  await settled();
  assert.equal(queue.failed.length, 1);
  assert.equal(queue.pending, 0);
  assert.equal(queue.discard(firstId), true);
  assert.equal(queue.failed.length, 0);
  assert.deepEqual(states.at(-1), [0, 0]);
  assert.equal(queue.discard(firstId), false);
  assert.equal(queue.discard(secondId), false);
});

test("UI callback errors cannot turn a saved action into a failed or stalled request", async () => {
  const { Queue, errors } = loadQueue();
  const sent = [];
  const queue = new Queue({
    send: (op) => sent.push(op),
    onChange: () => {
      throw new Error("Rendering failed");
    },
    onSuccess: () => Promise.reject(new Error("Refresh failed")),
  });
  queue.enqueue("first");
  queue.enqueue("second");
  await settled();
  assert.deepEqual(sent, ["first", "second"]);
  assert.equal(queue.pending, 0);
  assert.equal(queue.failed.length, 0);
  assert.ok(errors.some((error) => error.message === "Rendering failed"));
  assert.ok(errors.some((error) => error.message === "Refresh failed"));
});
