const test = require("node:test");
const assert = require("node:assert/strict");

const {
  canAcceptAbortedSameOriginNavigation,
} = require("../src/navigation-load-policy.cjs");

test("accepts an app-owned redirect that aborts the initial Electron navigation", () => {
  const error = new Error("ERR_ABORTED (-3) loading agreement");
  assert.equal(
    canAcceptAbortedSameOriginNavigation(
      error,
      "http://127.0.0.1:3000/agreement?redirect=%2F",
      "http://127.0.0.1:3000/"
    ),
    true
  );
});

test("does not hide real failures or redirects outside the app origin", () => {
  assert.equal(
    canAcceptAbortedSameOriginNavigation(
      new Error("ERR_CONNECTION_REFUSED"),
      "http://127.0.0.1:3000/",
      "http://127.0.0.1:3000/"
    ),
    false
  );
  assert.equal(
    canAcceptAbortedSameOriginNavigation(
      new Error("ERR_ABORTED"),
      "https://example.com/",
      "http://127.0.0.1:3000/"
    ),
    false
  );
  assert.equal(
    canAcceptAbortedSameOriginNavigation(
      Object.assign(new Error("navigation stopped"), { errno: -3 }),
      "about:blank",
      "http://127.0.0.1:3000/"
    ),
    false
  );
});
