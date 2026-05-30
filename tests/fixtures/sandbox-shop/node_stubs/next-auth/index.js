// Vendored STUB for the `next-auth` dependency.
//
// This is a trivial, dependency-free stand-in for the real `next-auth` package
// so the sandbox app is runnable WITHOUT `npm install`. The fileak engine's
// `broken_token_service` profile swaps this dependency for a THROW_UNHANDLED
// mock by rewriting package.json (dep spec -> file:./.fileak_mocks/next-auth),
// so what matters to the engine is that `next-auth` is a declared dependency.
//
// The stub fails gracefully: a healthy token verification returns a demo user.
"use strict";

module.exports = {
  // Pretend to verify an auth token. The real lib does crypto/JWT work; here we
  // just return a benign demo session so the happy path renders a 200 page.
  verify(token) {
    return { user: "demo-user", token: token || "demo-token" };
  },
};
