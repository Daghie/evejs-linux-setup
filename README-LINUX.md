# EveJS v0.12.7.1 Universal Linux Setup v1.12 — Stock-Only Edition

This package installs or transactionally updates **clean EveJS v0.12.7.1** on Linux. It intentionally has **no Living Universe installation option**.

## Installer menu

1. Install clean EveJS v0.12.7.1
2. Update clean EveJS v0.12.7 → v0.12.7.1
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

For the first acceptance run choose **1**, use the supplied official `EveJS-v0.12.7.1.zip`, a copied EVE 24.01/build-3396210 client, your normal official Steam Proton, and a new empty EveJS destination. Market v1 remains the recommended first test.

Classification: **SOURCE AUDITED / STATIC VALIDATED / PACKAGE GENERATED / LIVE BAZZITE ACCEPTANCE PENDING**.
