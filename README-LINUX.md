# EveJS v0.12.9 Universal Linux Setup v1.14 — Stock-Only Edition

This pre-release folder installs or transactionally updates **clean EveJS v0.12.9** on Linux. It intentionally has **no Living Universe installation option**.

## Installer menu

1. Install clean EveJS v0.12.9
2. Update clean EveJS v0.12.8 → v0.12.9
3. Prepare/verify clean runtime compatibility
4. Restore an upgrade backup
5. Manage upgrade backups
6. Exit

Living Universe, X-Patch and unknown integrated source states fail closed. Ordinary `mods/*/loader.js` mods remain supported, except the legacy Living Universe loader, which remains disabled.

## v0.12.9 Linux migration

EveJS v0.12.9 moves generated TLS material to `_local/certs` and requires a constrained local CA, separate DNS/numeric gateway leaves, and exact-hash offline patches for the supported EVE client. Setup v1.14 performs those operations for a fresh install. After a v0.12.8 transactional update, the first v1.14 launcher start performs the CA/client migration before starting EveJS.

The client patch gate accepts only the audited build-3396210 originals, the audited earlier numeric-loopback LaunchDarkly patch, or the final audited patched hashes. Unexpected binaries fail closed. Original files and changed CA bundles receive backups.

## Live verification

```bash
chmod +x install-evejs-linux.sh
./install-evejs-linux.sh
```

For a fresh installation, choose **1**, select the official `EveJS-v0.12.9.zip`, a separate copied EVE 24.01/build-3396210 client and resource cache, your normal official Steam Proton, and a new empty EveJS destination. Market v1 remains the recommended first profile.

For an existing v0.12.8 Universal Linux installation, choose **2**. The update preserves the complete `_local` tree, Market data, mods, certificates, configuration and logs; it does not rebuild the world or reseed the Market.

Static/package verification completed on 2026-09-25. Real backend, Proton/client UI, update/rollback and cleanup acceptance for v0.12.9 remain pending the requested live verification. No distribution ZIP is produced until that verification is accepted.
