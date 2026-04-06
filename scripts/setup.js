#!/usr/bin/env node
/**
 * scripts/setup.js
 *
 * Creates node_modules/.bin/trader → trader-cli/main.py symlink.
 * Runs automatically via `npm install` (postinstall hook).
 * Can also be run manually: node scripts/setup.js
 *
 * Advantages over the shell one-liner it replaces:
 *   - No reliance on ln / chmod shell commands (safer on varied environments)
 *   - Explicit error messages with exit code 1 on real failures
 *   - Idempotent: safe to run multiple times
 */

'use strict';

const fs   = require('fs');
const path = require('path');

const PROJECT_ROOT = path.resolve(__dirname, '..');
const TARGET       = path.join(PROJECT_ROOT, 'trader-cli', 'main.py');
const BIN_DIR      = path.join(PROJECT_ROOT, 'node_modules', '.bin');
const LINK         = path.join(BIN_DIR, 'trader');

// ── Validate target exists ────────────────────────────────────────────────────
if (!fs.existsSync(TARGET)) {
  console.error(`❌ setup.js: target not found: ${TARGET}`);
  process.exit(1);
}

// ── Ensure execute bit (owner + group + other read/execute) ───────────────────
try {
  fs.chmodSync(TARGET, 0o755);
} catch (err) {
  // Non-fatal: file may already have correct perms or be on a fs that ignores chmod
  console.warn(`⚠️  setup.js: could not chmod ${TARGET}: ${err.message}`);
}

// ── Ensure bin directory exists ───────────────────────────────────────────────
fs.mkdirSync(BIN_DIR, { recursive: true });

// ── Remove stale link / file before re-creating ───────────────────────────────
try {
  fs.unlinkSync(LINK);
} catch (err) {
  if (err.code !== 'ENOENT') {
    console.error(`❌ setup.js: could not remove existing link: ${err.message}`);
    process.exit(1);
  }
}

// ── Create symlink ────────────────────────────────────────────────────────────
try {
  fs.symlinkSync(TARGET, LINK);
} catch (err) {
  console.error(`❌ setup.js: symlink creation failed: ${err.message}`);
  process.exit(1);
}

console.log(`✅ trader CLI linked → node_modules/.bin/trader`);

