# EveJS v0.12.8 Universal Linux Setup v1.13 — Stock-Only Edition

This package installs or transactionally updates **clean EveJS v0.12.8** on Linux. It intentionally has **no Living Universe installation option**.

## Installer menu

1. Install clean EveJS v0.12.8
2. Update clean EveJS v0.12.7.1 → v0.12.8
3. Prepare/verify clean runtime compatibility
4. Restore an upgrade backup
5. Manage upgrade backups
6. Exit

Living Universe, X-Patch and unknown integrated source states fail closed in this edition. Ordinary `mods/*/loader.js` mods continue to be supported, except the legacy Living Universe loader which remains disabled.

## First live test

```bash
chmod +x install-evejs-linux.sh
./install-evejs-linux.sh
```

For a fresh installation choose **1**, select the official `EveJS-v0.12.8.zip`, a copied EVE 24.01/build-3396210 client, your normal official Steam Proton, and a new empty EveJS destination. Market v1 remains the recommended first profile.

Acceptance completed on 2026-09-14: real Node 24 dependency installs/native smoke, static database generation from the build-3396210 SDE, v1 Market seed/build/doctor, 210-service/9-content-pack backend boot, full launcher-owned port/TLS startup with a harmless client stand-in, transactional update, rollback-backup verification, restore, and graceful process cleanup all passed. The physical EVE executable was not available for a separate Proton/UI smoke; its v0.12.8 ClientSETUP and build-3396210 patch contract are byte-identical to v0.12.7.1.
