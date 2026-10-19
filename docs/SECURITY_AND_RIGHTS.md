# Security and dependency scope

## Offline boundary

The default demo reads invented packaged data and writes a deterministic report. It has no broker SDK, account credentials, live order commands or external data requests. Persistent simulated state is exercised in temporary test directories; runtime files are excluded from the source manifest.

The optional playback server binds loopback only. It checks Host and Origin, requires a session token for API reads, bounds request bodies and rejects writes. Responses use nonce CSP and no-store headers; embedded JSON and displayed strings are escaped. Request paths cannot load arbitrary files. Standalone playback embeds the same immutable report without external scripts, fonts or chart libraries.

## Checks and limits

The full suite includes real loopback HTTP checks, malformed requests, unsafe payloads, ledger restart/corruption and independent cash reconciliation. Secret scanning uses Gitleaks with redacted output. Manual review covers configuration, fixtures, rendered images, documentation and commit identity. These checks do not guarantee absence of every possible vulnerability.

Runtime and tests use only Python's standard library. Reference Python 3.12.14 and Windows compatibility Python 3.14.8 are pinned in CI; optional build tooling is setuptools 84.0.0. OSV queries for setuptools 84.0.0 and preparation pip 26.2.1 returned no listed advisories on 19 October 2026. That dated package query does not audit the entire interpreter or operating system. CI actions are pinned to full commit IDs with read-only repository permissions.

## Rights

Code/documentation are MIT licensed; invented observations are CC0 1.0. See LICENSE, LICENSE-DATA, RIGHTS.md and THIRD_PARTY_NOTICES.md. No third-party market observations are distributed. Future market datasets require separate terms and provenance. Source files and their SHA-256 hashes are enumerated in release-manifest.json.
