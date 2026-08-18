# EveJS v0.12.6 — Universal Linux Installer v1.7

v1.7 ports the accepted Linux installer/launcher architecture to **EveJS
v0.12.6** while retaining the SteamOS minimal-native safeguards introduced in
v1.6.

## What is preserved

- graphical Zenity/KDialog installer with terminal fallback;
- retrying selectors for invalid ZIP/client/destination choices;
- user-selected existing empty destination root;
- Steam Proton discovery and isolated Proton prefix;
- EVE build 3396210 client validation and patching;
- native host build so EveJS mods use the same environment as the server;
- `mods/*/loader.js` loading through Node `--require`;
- stale EveJS process-tree cleanup;
- Market-before-Game startup ordering;
- process-tree ownership checks for every required service;
- XMPP `127.0.0.1` Linux/Proton correction;
- EVE client CA reconciliation and Wine-visible CA path;
- XMPP and Gateway TLS verification before EVE launches.

## SteamOS minimal-native repair

v1.7 retains v1.6's conservative SteamOS behavior.

It does **not** use:

```text
steamos-unminimize --dev
base-devel group repair
pacman -Sy
pacman -Syu
containerized EveJS builds
```

If SteamOS has pruned a required native development file, real C/C++ probes
identify the failure.

A mapped package can be reinstalled only if:

1. that package is already installed;
2. the installed version exactly matches the configured SteamOS repository
   version;
3. Pacman's dry-run contains exactly that **one package**;
4. the user explicitly approves it;
5. the one-package preview is repeated immediately before the write.

For the known `stdio.h` failure, the first candidate is only `glibc`.

## v0.12.6 source audit highlights

The following remain compatible with the previous Linux release:

- EVE client build: `3396210`;
- `blue.dll` patch recipe: unchanged;
- Node 24 LTS recommendation: unchanged;
- native setup audit: unchanged;
- startup batch scripts: unchanged;
- client certificate setup: unchanged;
- XMPP/Game/Image/Gateway/Redshift ports: unchanged;
- Market HTTP/RPC ports: `40110/40111`.

v0.12.6 changes that the installer intentionally preserves:

- the Market daemon now enforces loopback internally;
- Market v1's shipped stock quantity is now `9,999,999` per station/type;
- Market v3 includes decryptors in its junk-seed category;
- Docker explicitly publishes Market RPC `40111`;
- the shipped PlayerConnect development token defaults to blank.

None of those require a new Proton/TLS/process-ownership architecture.

## Market choices

For the first v0.12.6 acceptance run choose:

```text
v1 — Synthetic Jita + New Caldari
```

It is deterministic/offline and removes an external live snapshot as a test
variable.

The installer also supports the shipped v2 and v3 seeders.

## Installation

Recommended package:

```bash
tar -xzf EveJS-v0.12.6-Universal-Linux-Installer-v1.7.tar.gz
cd EveJS-v0.12.6-Universal-Linux-Installer-v1.7
./install-evejs-linux.sh
```

Select the original `EveJS-v0.12.6.zip` release when requested.

## Acceptance status

```text
SOURCE AUDITED
STATIC VALIDATED
LIVE v0.12.6 ACCEPTANCE PENDING
```

Do not treat v1.7 as the new protected live baseline until a fresh install,
first launch, launcher restart, gameplay session, and log review pass.
