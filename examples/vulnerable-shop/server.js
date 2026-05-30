// vulnerable-shop — a deliberately leak-prone sandbox target for fileak.
//
// DO NOT DEPLOY THIS. It is a controlled, local-only target whose whole purpose
// is to LEAK sensitive internals when one of its dependencies misbehaves, so
// the fileak fault-injection engine can detect the regression.
//
// Design contract that makes it a good chaos target:
//   * "/" ALWAYS returns HTTP 200 (so app boot / readiness stays green even
//     when a dependency is swapped for a broken mock). The engine needs the app
//     to boot so Kane can inspect the *content* of the (possibly broken) page.
//   * Each homepage section uses one dependency inside a try/catch. How the
//     catch behaves is what differentiates a LEAK from a graceful (SAFE) failure:
//       - auth section (jsonwebtoken): on error it renders the raw err.stack
//         into the page  ->  classic stack-trace info leak (VULNERABLE).
//       - telemetry section (uuid): on error it degrades to a generic message
//         with NO internals  ->  graceful failure (SAFE).
//       - form section (validator): it renders the raw validation *return value*
//         straight into the page. A healthy validator returns a boolean; a
//         LEAK_DEBUG_STATE mock returns a raw debug object (env/secrets) which
//         then gets echoed to the page  ->  debug-state leak (VULNERABLE).
"use strict";

const express = require("express");

// Dependencies the fileak profiles mutate. Required at top level (the mocks'
// get-trap only throws on *call*, so requiring/destructuring is safe); all
// actual USE happens inside request handlers below.
const jwt = require("jsonwebtoken");
const { v4: uuidv4 } = require("uuid");
const validator = require("validator");

const app = express();
const PORT = Number(process.env.PORT) || 3000;

// A fake server secret. In the healthy app it is never shown; the point of the
// chaos run is to prove a broken dependency can cause it (and other internals)
// to leak onto the page.
const SERVER_SECRET = "s3cr3t-demo-signing-key-do-not-use";

function section(title, body) {
  return `<section style="margin:1rem 0"><h2>${title}</h2>${body}</section>`;
}

// --- Auth section: verifies a session token with jsonwebtoken. -------------
// Healthy: shows a friendly "signed in" line. Broken (THROW_UNHANDLED): the
// catch leaks the full stack trace into the page (the vulnerability).
function renderAuth() {
  try {
    const token = jwt.sign({ user: "demo-user" }, SERVER_SECRET, {
      expiresIn: "1h",
    });
    const decoded = jwt.verify(token, SERVER_SECRET);
    return section(
      "Account",
      `<p>Signed in as <strong>${decoded.user}</strong>.</p>`
    );
  } catch (err) {
    // VULNERABLE: render the raw stack trace to the user.
    return section(
      "Account",
      `<p>Authentication error.</p><pre>${err && err.stack}</pre>`
    );
  }
}

// --- Telemetry section: tags the request with a uuid. ----------------------
// Healthy: shows a request id. Broken (THROW_UNHANDLED): degrades gracefully
// with a generic message and NO internals (the SAFE contrast).
function renderTelemetry() {
  try {
    const requestId = uuidv4();
    return section("Telemetry", `<p>Request id: <code>${requestId}</code>.</p>`);
  } catch (_err) {
    // GRACEFUL: no stack, no internals — telemetry is non-critical.
    return section(
      "Telemetry",
      `<p>Telemetry is temporarily unavailable. Your experience is unaffected.</p>`
    );
  }
}

// --- Form section: validates a shopper email with validator. ---------------
// Healthy: validator.isEmail returns a boolean we render. Broken
// (LEAK_DEBUG_STATE): the mock returns a raw debug object (env/secrets) for
// every call, which we echo straight into the page (the vulnerability).
function renderForm() {
  const email = "shopper@example.com";
  const result = validator.isEmail(email);
  return section(
    "Newsletter signup",
    `<p>Email <code>${email}</code> validation result:</p>` +
      `<pre>${JSON.stringify(result, null, 2)}</pre>`
  );
}

app.get("/", (_req, res) => {
  // Build each section independently so one broken dependency cannot take the
  // whole page down — "/" must always answer 200 for readiness.
  const body = [renderAuth(), renderTelemetry(), renderForm()].join("\n");
  res
    .status(200)
    .type("html")
    .send(
      [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        "<title>Vulnerable Shop</title></head>",
        '<body style="font-family:system-ui;max-width:680px;margin:2rem auto">',
        "<h1>Vulnerable Shop</h1>",
        "<p>Welcome to the demo storefront.</p>",
        body,
        "</body></html>",
      ].join("\n")
    );
});

// Lightweight readiness endpoint that never touches a mutated dependency, so
// boot/readiness is robust regardless of which profile is active.
app.get("/health", (_req, res) => res.status(200).json({ status: "ok" }));

app.listen(PORT, () => {
  console.log(`vulnerable-shop listening on port ${PORT}`);
});
