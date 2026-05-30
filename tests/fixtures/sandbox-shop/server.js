// Tiny sandbox target app for the fileak fault-injection engine.
//
// This is a deliberately minimal Node HTTP server (built-in `http` only — no
// framework) that exercises the three dependencies the engine's default
// profiles mutate:
//
//   next-auth  -> broken_token_service  (THROW_UNHANDLED on the auth dep)
//   analytics  -> crashed_telemetry     (THROW_UNHANDLED on the analytics dep)
//   validator  -> compromised_input_handler (LEAK_DEBUG_STATE on the form dep)
//
// When the engine swaps one of these for a broken mock and reboots the app, the
// failure becomes observable in the response / process output — which is what
// Kane CLI inspects. In the healthy baseline below every dependency behaves, so
// the app serves a clean 200 page with no leaked internals.
//
// `require` uses the REAL package names so the engine's package.json mutation is
// what matters. Because no `node_modules` is committed, each require falls back
// to the vendored stub under ./node_stubs/<name> so the app is runnable as-is
// for a local smoke test or a BYO operator (this file does NOT run in CI).
"use strict";

const http = require("http");
const path = require("path");

// Resolve a dependency by its real package name, falling back to the vendored
// stub when it is not installed in node_modules. After the engine runs
// `npm install`, the real name resolves to the engine's mock instead.
function load(pkg) {
  try {
    return require(pkg);
  } catch (err) {
    return require(path.join(__dirname, "node_stubs", pkg));
  }
}

const auth = load("next-auth");
const analytics = load("analytics");
const validator = load("validator");

const PORT = Number(process.env.PORT) || 3000;

const server = http.createServer((req, res) => {
  // Touch each dependency on every request so a broken mock surfaces here.
  const session = auth.verify("demo-token"); // broken_token_service mutates this
  analytics.track("page_view"); // crashed_telemetry mutates this
  const emailOk = validator.isEmail("shopper@example.com"); // compromised_input_handler mutates this

  res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
  res.end(
    [
      "<!doctype html>",
      "<html><head><title>Sandbox Shop</title></head><body>",
      "<h1>Sandbox Shop</h1>",
      "<p>Welcome, " + session.user + ".</p>",
      "<p>Checkout is open. Email valid: " + emailOk + ".</p>",
      "</body></html>",
    ].join("\n")
  );
});

server.listen(PORT, () => {
  // Plain readiness line — intentionally free of stack traces, file paths, SQL,
  // env-var assignments, or secrets so a healthy boot stays "SAFE".
  console.log("sandbox-shop listening on port " + PORT);
});
