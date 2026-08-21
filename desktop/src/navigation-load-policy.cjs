"use strict";

function isAbortedNavigationError(error) {
  const code = String(error?.code || "").trim().toUpperCase();
  const message = String(error?.message || error || "").toUpperCase();
  return code === "ERR_ABORTED" || error?.errno === -3 || message.includes("ERR_ABORTED");
}

function isSameHttpOrigin(currentUrl, requestedUrl) {
  try {
    const current = new URL(String(currentUrl || ""));
    const requested = new URL(String(requestedUrl || ""));
    return (
      (current.protocol === "http:" || current.protocol === "https:") &&
      current.origin === requested.origin
    );
  } catch {
    return false;
  }
}

function canAcceptAbortedSameOriginNavigation(error, currentUrl, requestedUrl) {
  return isAbortedNavigationError(error) && isSameHttpOrigin(currentUrl, requestedUrl);
}

module.exports = {
  canAcceptAbortedSameOriginNavigation,
  isAbortedNavigationError,
  isSameHttpOrigin,
};
