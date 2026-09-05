# EveJS v0.12.7.1 Universal Linux Setup v1.12 — Living Universe Edition

This package installs or updates EveJS v0.12.7.1 and can transactionally install the supplied **Living Universe** authority. It does **not** use the stale X-Patch packaging contained in the development tree.

## Installer menu

1. Install EveJS v0.12.7.1
2. Update EveJS v0.12.7 → v0.12.7.1
3. Install/verify Living Universe or prepare runtime compatibility
4. Restore an upgrade backup
5. Manage upgrade backups
6. Exit

A fresh install offers **Keep clean EveJS** or **Install Integrated Living Universe** at the end. Option 3 can add LU to an existing clean v0.12.7.1 Universal Linux installation.

The updater can also migrate the exact prior **v1.11 installer-managed LU on EveJS v0.12.7** after the prior installed authority verifies successfully. Old X-Patch and unmanaged/partial LU states remain fail closed.

## LU Monitor from the Launcher

When Integrated LU is installed and verified, the Launcher includes:

`[L] Start/stop LU Monitor`

The Monitor runs separately at **http://127.0.0.1:26050/** and has its own launcher log. The existing EveJS service on port 26400 is labeled **Redshift**, not Monitor.

The Monitor can read its world database while the Game server is stopped; LU write actions still require the running LU server bridge, as defined by the supplied Monitor itself.

## First live test

```bash
chmod +x install-evejs-linux.sh
./install-evejs-linux.sh
```

For the safest first test choose **1**, install into a new empty EveJS directory, choose Market v1, then choose **Install Integrated Living Universe**. After the normal stack launches, press **L** in the Launcher to start the LU Monitor.

Classification: **SOURCE AUDITED / LU PAYLOAD AUDITED / STATIC VALIDATED / PACKAGE GENERATED / LIVE BAZZITE ACCEPTANCE PENDING**.
