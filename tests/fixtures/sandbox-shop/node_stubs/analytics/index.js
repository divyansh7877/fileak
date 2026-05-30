// Vendored STUB for the `analytics` dependency.
//
// Trivial, dependency-free stand-in for the real `analytics` package so the
// sandbox app runs WITHOUT `npm install`. The fileak engine's
// `crashed_telemetry` profile swaps this dependency for a THROW_UNHANDLED mock
// (dep spec -> file:./.fileak_mocks/analytics) to simulate telemetry crashing
// globally; the healthy stub below just no-ops.
"use strict";

module.exports = {
  // Record a page/usage event. The healthy stub silently succeeds so telemetry
  // never affects the happy-path response.
  track(event) {
    return { ok: true, event: event || "page_view" };
  },
};
