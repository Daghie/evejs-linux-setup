# EveJS v0.12.6 — Universal Linux Setup v1.8.1
## Installer + Transactional Updater


## v1.8.1 hotfix

The first live v0.12.5 → v0.12.6 update test exposed a Cargo proxy-path bug.

Rustup normally installs:

```text
~/.cargo/bin/cargo -> rustup
```

v1.8 converted the configured Cargo path with Python `Path.resolve()`. That
dereferenced the valid Cargo proxy to the underlying `rustup` executable, so the
Market build accidentally became:

```text
rustup build --release
```

instead of:

```text
cargo build --release
```

v1.8.1 keeps the executable path without dereferencing symlinks and verifies
that `<path> --version` actually identifies itself as Cargo before staging can
continue.

The failed run was **not caused by a mod**. Update mode explicitly preserves the
entire existing `mods/` tree. v1.8.1 strengthens this with an exact manifest
comparison (files, hashes, symlinks, modes and empty directories) between the
existing and staged `mods/` trees. Any mod change aborts the update before
commit.

v1.8.1 also:

- removes a newly-created staging directory when pre-commit staging fails;
- smoke-tests `better-sqlite3` and `protobufjs` after target server `npm ci`;
- retains the launcher byte-for-byte from v1.7.

v1.8 adds **update/rollback management to the Installer**.

The normal EveJS launcher is intentionally unchanged from v1.7. It does not
contain an Update option and does not manage upgrade backups.

## Opening menu

GUI and text-console variants expose the same setup functions:

```text
[1] Install EveJS v0.12.6
[2] Update an existing EveJS installation
[3] Restore an upgrade backup
[4] Manage upgrade backups
[5] Exit
```

## Fresh Install

The existing v1.7 fresh-install path is retained:

- Steam Proton discovery;
- EVE build 3396210 preparation;
- Market v1/v2/v3;
- process-tree ownership launcher;
- TLS/XMPP Linux fixes;
- SteamOS single-package native repair;
- removable `mods/*/loader.js` support.

## Audited Update: v0.12.5 → v0.12.6

v1.8 currently supports exactly one in-place upgrade contract:

```text
EveJS v0.12.5 → EveJS v0.12.6
EVE build 3396210 → 3396210
```

Other source versions are refused instead of guessed.

### Preserved

- `_local/gameStore/gamestore.sqlite` player/world state;
- the existing Market `market.sqlite`, including player orders/history;
- all other `_local` content, including mod-owned runtime state;
- `mods/`;
- current local certificates;
- `.evejs-linux.conf`;
- the copied EVE client;
- the existing Proton prefix;
- logs.

### Refreshed

- EveJS program/source tree;
- root/server Node dependencies;
- v0.12.6 generated static game data + manifest;
- v0.12.6 Market daemon binary;
- launcher/cleanup files from the current package.

### Explicitly NOT done during an update

- Market reseed;
- new player database;
- new Proton prefix;
- unnecessary EVE-client repatch.

## Configuration merge

The update contains the audited v0.12.5 default config snapshot and performs a
three-way merge:

```text
old v0.12.5 default
        +
user's existing config
        +
new v0.12.6 default
```

Rules:

- unchanged old setting → adopt new EveJS default;
- user-changed setting → preserve user value;
- new v0.12.6 setting → add it;
- if both user and EveJS changed the same setting → preserve the user value and
  record a merge conflict.

`CONFIG-MERGE-REPORT.txt` records what happened.

This means the new v0.12.6 combat setting is added automatically, while a custom
PlayerConnect token is retained rather than overwritten.

## Transactional filesystem update

The working installation is not overwritten while the new version is built.

v1.8 first creates a complete staging tree beside the installation, installs
dependencies there, rebuilds static authority, compiles the new Market daemon,
and validates SQLite conservation.

Only after validation:

```text
existing EveJS folder
        ↓ atomic rename
full rollback backup

validated v0.12.6 staging folder
        ↓ atomic rename
original EveJS path
```

The user's EveJS path therefore stays exactly the same after a successful update.

## Conservation checks

Before and after staging, the updater checkpoints and runs SQLite
`integrity_check` on:

- `gamestore.sqlite`;
- `market.sqlite`.

Their SHA-256 hashes must remain identical across the update.

If either changes unexpectedly, the update stops before the existing
installation is replaced.

## Backups

Upgrade backups are stored beside the EveJS installation:

```text
<parent>/.evejs-upgrade-backups/<installation-name>/
```

The old complete installation is retained as the rollback backup.

The Installer can later:

- inspect a backup;
- delete a backup;
- restore a backup.

A restore preserves the currently installed version as an additional safety
backup before replacing it.

## Automatic rollback

After the filesystem swap, v1.8 validates:

- target EveJS version/build;
- JSON config parsing;
- launcher/cleanup Bash syntax;
- player/world SQLite integrity;
- Market SQLite integrity;
- exact DB conservation hashes;
- new Market daemon `doctor`.

If that validation fails, the old installation is moved back to its original
path automatically.

## Launcher boundary

`evejs-linux-launcher.sh` in this package is byte-for-byte identical to the v1.7
launcher.

It remains runtime-only:

- Market start/stop;
- Game start/stop;
- EVE client start/stop;
- restart/reload mods;
- diagnostics;
- paths/logs;
- stale-process handling.

No updater option is present.

## Usage

```bash
tar -xzf EveJS-v0.12.6-Universal-Linux-Setup-v1.8-Installer-Updater.tar.gz
cd EveJS-v0.12.6-Universal-Linux-Setup-v1.8-Installer-Updater
./install-evejs-linux.sh
```

Choose Install, Update, Restore, or Manage Backups.

## Status

```text
SOURCE/STATIC VALIDATED
UPDATER MERGE/TRANSACTION SELF-TESTED
LIVE FRESH-INSTALL ACCEPTANCE PENDING FOR v1.8 WRAPPER
LIVE v0.12.5 → v0.12.6 UPDATE ACCEPTANCE PENDING
```
