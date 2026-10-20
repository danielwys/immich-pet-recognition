(function (global) {
  "use strict";

  class PetActionQueue {
    constructor({ send, onChange, onSuccess, onError } = {}) {
      if (typeof send !== "function")
        throw new TypeError("PetActionQueue requires a send function.");
      this._send = send;
      this._onChange = onChange;
      this._onSuccess = onSuccess;
      this._onError = onError;
      this._waiting = [];
      this._failed = new Map();
      this._active = null;
      this._sequence = 0;
      this._scheduled = false;
      this._draining = false;
    }

    get pending() {
      return this._waiting.length + Number(this._active !== null);
    }

    get running() {
      return this._active !== null;
    }

    get failed() {
      return Array.from(this._failed.values(), ({ id, op, error }) => ({
        id,
        op,
        error,
      }));
    }

    enqueue(op) {
      const id = `action-${++this._sequence}`;
      this._waiting.push({ id, op });
      this._notify(this._onChange, this);
      this._schedule();
      return id;
    }

    retry(id) {
      const failed = this._failed.get(id);
      if (!failed) return false;
      this._failed.delete(id);
      this._waiting.push({ id, op: failed.op });
      this._notify(this._onChange, this);
      this._schedule();
      return true;
    }

    discard(id) {
      if (!this._failed.delete(id)) return false;
      this._notify(this._onChange, this);
      return true;
    }

    _notify(callback, ...args) {
      if (typeof callback !== "function") return;
      const report = (error) => {
        if (typeof global.reportError === "function") global.reportError(error);
        else if (global.console) global.console.error(error);
      };
      try {
        const result = callback(...args);
        if (result && typeof result.catch === "function") result.catch(report);
      } catch (error) {
        report(error);
      }
    }

    _schedule() {
      if (this._scheduled || this._draining) return;
      this._scheduled = true;
      Promise.resolve().then(() => {
        this._scheduled = false;
        return this._drain();
      });
    }

    async _drain() {
      if (this._draining) return;
      this._draining = true;
      try {
        while (this._waiting.length) {
          const entry = this._waiting.shift();
          this._active = entry;
          this._notify(this._onChange, this);
          let result;
          let error;
          let succeeded = false;
          try {
            result = await this._send(entry.op);
            succeeded = true;
          } catch (failure) {
            error = failure;
            this._failed.set(entry.id, { ...entry, error });
          }
          this._active = null;
          if (succeeded) this._notify(this._onSuccess, entry.op, result);
          else this._notify(this._onError, entry.op, error);
          this._notify(this._onChange, this);
        }
      } finally {
        this._active = null;
        this._draining = false;
      }
    }
  }

  global.PetActionQueue = PetActionQueue;
})(window);
