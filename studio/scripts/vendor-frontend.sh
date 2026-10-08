#!/usr/bin/env bash
# Download and verify browser runtime dependencies from an explicitly configured npm mirror.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NPM_REGISTRY_URL="${NPM_REGISTRY_URL:-}"
UPDATE_HASHES=0

if [[ "${1:-}" == "--update-hashes" ]]; then
  UPDATE_HASHES=1
  shift
fi
if [[ "$#" -ne 0 ]]; then
  echo "usage: bash scripts/vendor-frontend.sh [--update-hashes]" >&2
  exit 2
fi
if [[ -z "$NPM_REGISTRY_URL" ]]; then
  echo "NPM_REGISTRY_URL is required; use a configured domestic npm registry" >&2
  exit 2
fi
if [[ "$NPM_REGISTRY_URL" != https://* ]]; then
  echo "NPM_REGISTRY_URL must be an HTTPS registry URL" >&2
  exit 2
fi
if ! command -v npm >/dev/null 2>&1 || ! command -v node >/dev/null 2>&1 || ! command -v tar >/dev/null 2>&1; then
  echo "npm, node, and tar are required to refresh frontend vendor assets" >&2
  exit 2
fi

node - "$ROOT" "$NPM_REGISTRY_URL" "$UPDATE_HASHES" <<'NODE'
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const [root, registry, updateArg] = process.argv.slice(2);
const updateHashes = updateArg === '1';
const manifestPath = path.join(root, 'frontend', 'vendor', 'manifest.json');
const vendorRoot = path.join(root, 'frontend', 'vendor');
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
const workDir = fs.mkdtempSync(path.join(os.tmpdir(), 'quicstudio-vendor-'));

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function ensureRelative(value, label) {
  if (!value || path.isAbsolute(value) || value.split(/[\\/]/).includes('..')) {
    throw new Error(`invalid ${label}: ${value}`);
  }
}

try {
  for (const pkg of manifest.packages) {
    const packed = JSON.parse(execFileSync(
      'npm',
      ['pack', `${pkg.name}@${pkg.version}`, '--registry', registry, '--ignore-scripts', '--json'],
      { cwd: workDir, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
    ));
    const tarball = packed[0]?.filename;
    if (!tarball) throw new Error(`npm pack did not return a tarball for ${pkg.name}@${pkg.version}`);

    const extractDir = path.join(workDir, `${pkg.name}-${pkg.version}`);
    fs.mkdirSync(extractDir, { recursive: true });
    execFileSync('tar', ['-xzf', path.join(workDir, tarball), '-C', extractDir], { stdio: 'inherit' });

    for (const asset of pkg.assets) {
      ensureRelative(asset.source, 'package asset source');
      ensureRelative(asset.target, 'vendor asset target');
      const source = path.join(extractDir, 'package', asset.source);
      if (!fs.statSync(source).isFile()) throw new Error(`missing ${asset.source} in ${pkg.name}@${pkg.version}`);
      const actual = sha256(source);
      if (!updateHashes && asset.sha256 !== actual) {
        throw new Error(`SHA-256 mismatch for ${pkg.name}@${pkg.version}/${asset.target}`);
      }
      if (updateHashes) asset.sha256 = actual;

      const target = path.join(vendorRoot, pkg.name, pkg.version, asset.target);
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.copyFileSync(source, target);
      process.stdout.write(`vendored ${pkg.name}@${pkg.version}/${asset.target}\n`);
    }
  }

  if (updateHashes) {
    fs.writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');
    process.stdout.write('updated frontend/vendor/manifest.json SHA-256 values\n');
  }
} finally {
  fs.rmSync(workDir, { recursive: true, force: true });
}
NODE
