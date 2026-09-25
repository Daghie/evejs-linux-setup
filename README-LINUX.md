# EveJS v0.12.9 Universal Linux Setup v1.15 — SteamOS Portable, Stock-Only

This pre-release folder installs or transactionally updates **clean EveJS v0.12.9** on Linux. It intentionally has **no Living Universe installation option**.

## Platform behavior

- **SteamOS x86_64:** automatically uses the bundled, checksum-verified portable runtime. It does not require `make`, `cc`, `c++`, Cargo, `sudo`, Pacman, `steamos-readonly`, `sysctl`, or system-package changes. Node is installed only under the user's `~/.local/share/evejs-linux` folder when Node 24 is not already available. The local HTTPS/CDN responder uses unprivileged port 8443.
- **Other Linux distributions:** retain the established v1.14 behavior: npm installs the exact lockfiles, Rust components build locally, and the launcher uses its existing localhost port-443 path.

The SteamOS payload contains Node modules locked to the official v0.12.9 lockfiles, the Linux x64 `better-sqlite3` prebuild, and static-musl Market/seeder executables. The payload and each important native artifact are verified before installation or update.

## Installer menu

1. Install clean EveJS v0.12.9
2. Update clean EveJS v0.12.8 → v0.12.9
3. Prepare/verify clean runtime compatibility
4. Restore an upgrade backup
5. Manage upgrade backups
6. Exit

Living Universe, X-Patch and unknown integrated source states fail closed. Ordinary `mods/*/loader.js` mods remain supported, except the legacy Living Universe loader, which remains disabled.

## v0.12.9 Linux migration

EveJS v0.12.9 moves generated TLS material to `_local/certs` and requires a constrained local CA, separate DNS/numeric gateway leaves, and exact-hash offline patches for the supported EVE client.

On SteamOS, the exact-hash LaunchDarkly patch targets `https://localhost:8443`; the standard Linux profile continues to target `https://localhost` on port 443. The patch gate accepts only audited build-3396210 originals or known earlier/final variants, supports exact migration between the two profiles, and fails closed for unexpected binaries. Original files and changed CA bundles receive backups.

## Live verification

```bash
chmod +x install-evejs-linux.sh
./install-evejs-linux.sh
```

For a fresh installation, choose **1**, select the official `EveJS-v0.12.9.zip`, a separate copied EVE 24.01/build-3396210 client and resource cache, your normal official Steam Proton, and a new empty EveJS destination. Market v1 remains the recommended first profile.

For an existing v0.12.8 Universal Linux installation, choose **2**. The update preserves the complete `_local` tree, Market data, mods, certificates, configuration and logs; it does not rebuild the world or reseed the Market.

Static/package and portable-artifact verification completed on 2026-09-25. Real SteamOS backend, Proton/client UI, update/rollback and cleanup acceptance remain pending live verification. This deliverable intentionally remains a folder; no distribution ZIP is produced until that verification is accepted.
