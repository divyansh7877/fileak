// Vendored STUB for the `validator` dependency (the form-handling library).
//
// Trivial, dependency-free stand-in for the real `validator` package so the
// sandbox app runs WITHOUT `npm install`. The fileak engine's
// `compromised_input_handler` profile swaps this dependency for a
// LEAK_DEBUG_STATE mock (dep spec -> file:./.fileak_mocks/validator) which
// echoes raw internal debug state; the healthy stub below validates cleanly.
"use strict";

module.exports = {
  // Validate a form field value. The healthy stub returns a boolean only, never
  // echoing raw internal/debug objects back to the caller.
  isEmail(value) {
    return typeof value === "string" && /.+@.+\..+/.test(value);
  },
  // Sanitize a string for safe rendering (healthy: returns a trimmed string).
  escape(value) {
    return String(value == null ? "" : value).trim();
  },
};
