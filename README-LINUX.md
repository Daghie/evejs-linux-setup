# EveJS v0.12.7 — Universal Linux Setup v1.10

**Release classification:** SOURCE AUDITED · STATIC VALIDATED · PACKAGE GENERATED · LIVE v0.12.7 ACCEPTANCE PENDING

This package ports the accepted EveJS v0.12.6 Universal Linux Setup v1.9.1 architecture to the directly audited EveJS v0.12.7 source release. It is not a version-string-only port.

## What v1.10 supports

- Fresh installation of **EveJS v0.12.7** from the official release ZIP.
- Transactional update of an existing **Universal Linux EveJS v0.12.6 → v0.12.7** installation.
- Runtime-only Linux Launcher with the established Market → EveJS server → Proton client startup order.
- Preservation of player/world data, Market state, normal loader mods, logs, certificates, Linux configuration, Proton prefix and copied EVE client during the audited update.
- Preservation of v0.12.7 Content Packs under `_local/gameStore/content-packs/`.
- Source-matched external Market runtime caching keyed by the exact Market Rust source digest.

Direct **v0.12.5 → v0.12.7** updating is intentionally not implemented. Use the older audited v1.9.1 updater to reach v0.12.6 first, then use v1.10.

## Living Universe status on EveJS v0.12.7

No supplied Living Universe release has been proven compatible with v0.12.7 yet.

- Existing `mods/x-eve-living-universe/` files are preserved during a v0.12.6 → v0.12.7 update, but its `loader.js` is deliberately **not passed to Node** by the v1.10 Launcher.
- Ordinary loader mods under `mods/<mod-name>/loader.js` continue to load normally.
- X-Patch 0.3.2 explicitly targets EveJS v0.12.6. If its install state or known source overlay is detected, v1.10 **refuses the update before staging**. It never merges that overlay into v0.12.7.
- The fresh-install Living Universe prompt therefore keeps the clean v0.12.7 option available and marks both old LU variants unavailable until a separately compatible package is supplied and audited.

## Source facts established by the v0.12.6 → v0.12.7 audit

Input authorities:

- `EveJS-v0.12.6.zip` SHA-256: `b7dc63fd8ef9d482e27e73bbc1e8ec1c4d6bb2e8993807581bd3fa820dcb7182`
- `EveJS-v0.12.7.zip` SHA-256: `7cfb30b4c38b71293e6b83268509fd610794fb29b46aa4f1368b843c1fddf33b`
- v1.9.1 setup ZIP SHA-256: `511fd5b3c83f4f51d7a344361217eef641cbd8f45ad0f4ef3cb21366c4c0d027`

Full-tree comparison:

- ADDED: 20
- REMOVED: 0
- MODIFIED: 107
- UNCHANGED: 1,584

Directly proven unchanged between v0.12.6 and v0.12.7:

- EVE target: **24.01 build 3396210**.
- Complete `tools/ClientSETUP/` tree.
- `blue_patch_recipe.json` SHA-256 `970e9202e87e39e8529bec7071b482fb1fffa105e589472cf4b699c7723925e6`.
- Complete Rust `externalservices/market-server/` tree.
- All Market seeder tool trees (`market-seed`, `market-seederv2`, `market-seederv3`).
- `tools/Setup/`.
- `Play.bat`, `PlayDebug.bat`, `StartServer.bat`, `StartMarketServer.bat`.
- `config/server.json`, therefore the audited primary network/service defaults used by the Linux Launcher remain unchanged.
- Root/server dependency resolution after normalizing only the EveJS version metadata in the lockfiles.

The stock Market build-input identity therefore remains:

`E9164F59BA3140A4C1AA792D9BF82896D10ED266A43D1BCD062166F77DA9025A`

Directly proven changes that affect updating:

- `tools/DatabaseCreator/` changed and v0.12.7 adds static `dungeonClientContent`; generated static game data must be rebuilt for the target release.
- `config/mining.json` adds `industry.industryJobSpeedMultiplier = 1`.
- `config/npc.json` adds `targeting.npcPlayerDronesTargetable = true`.
- v0.12.7 adds persistent Content Packs under `_local/gameStore/content-packs/`; these are player-owned persistent state and are conserved exactly.
- v0.12.7 adds an industry installed-location migration which may durably update existing industry jobs on the **first v0.12.7 server start**. The updater itself does not start the game server during staging and preserves `gamestore.sqlite` byte-for-byte through the transaction.

See `SOURCE-AUDIT-v0.12.6-to-v0.12.7.txt` and `audit/` for machine-readable evidence.

## Installer / Launcher boundary

The **Installer** owns installation, updating, rollback backups, restore, prerequisite preparation, Market compilation and compatibility preparation.

The **Launcher is runtime-only**. It does not update EveJS, install Living Universe, repair Living Universe, create upgrade backups or restore backups.

## Bazzite / SteamOS safety

v1.10 retains the established conservative rules:

- no `steamos-unminimize --dev`;
- no `base-devel` bulk install;
- no `pacman -Sy` or `pacman -Syu` repair flow;
- no broad package floods;
- no containerized EveJS build workflow;
- native repair must be narrowed to the exact proven missing package and requires user approval.

It also retains the Cargo/rustup proxy fix: the Cargo command path is made absolute without dereferencing the rustup proxy symlink.

## Run the setup

Extract this package, then:

```bash
chmod +x install-evejs-linux.sh
./install-evejs-linux.sh
```

Opening menu:

1. Install EveJS v0.12.7
2. Update an existing EveJS installation
3. Prepare/verify v0.12.7 runtime compatibility
4. Restore an upgrade backup
5. Manage upgrade backups
6. Exit

The wizard uses Zenity or KDialog when available and falls back to a text console.

## First live acceptance test — fresh clean v0.12.7

Use a new empty destination. Select option **1**, point the installer at the exact `EveJS-v0.12.7.zip`, select the existing EVE 24.01 build 3396210 client copy, and choose an official Steam Proton. At the optional LU screen choose **Keep clean EveJS v0.12.7**.

After installation, launch EveJS normally and verify login/gameplay. Then return:

- `<EVEJS>/INSTALL-LINUX-SUMMARY.txt`
- `<EVEJS>/logs/launcher-linux/market-latest.log`
- `<EVEJS>/logs/launcher-linux/server-latest.log`
- `<EVEJS>/logs/launcher-linux/client-latest.log`
- any error shown in Konsole, if present

Do not perform the update test until the clean fresh-install launch has been reviewed.

## Later update acceptance test

After the fresh test passes, use option **2** against a disposable/copy-tested Universal Linux v0.12.6 installation with known persistent state. The updater will stage v0.12.7 beside the installation, validate it, atomically publish it, retain the entire previous installation as a rollback backup, and automatically restore the old installation if post-commit validation fails.

After the update, return:

- `<EVEJS>/UPDATE-LINUX-SUMMARY.txt`
- the update backup record/metadata path printed by the updater
- the three launcher logs above after the first v0.12.7 start

## Current validation boundary

This build has machine-verified source/archive comparison, syntax checks, compatibility classification, config merge fixtures, state-conservation staging fixtures and transaction rollback fixtures. The current execution sandbox does **not** contain Cargo/rustc, Node 24, the full external SDE/live client environment, Steam/Proton or Bazzite. Therefore a real Market compile/doctor, full static DB generation, native Node smoke under the intended Node 24 runtime, and live Proton launch have **not** been claimed here. Those remain part of the user-machine acceptance test.
