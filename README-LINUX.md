# EveJS Universal Linux Setup v1.17 — Stock-Only

Installs, updates and looks after **stock EveJS** (release 261018.1 or newer) on Linux, with a
copied EVE client running under Steam Proton. It has **no Living Universe installation option**.

## Platforms

- **Standard Linux (x86_64):** npm installs the release's exact lockfiles, the Rust Market
  programs are built locally, and the local HTTPS/CDN responder uses port 443.
- **SteamOS (x86_64):** uses the bundled portable runtime in `portable/steamos-x86_64/`
  (Node packages plus static Market/seeder programs). No compiler, Cargo, sudo, Pacman or
  read-only toggle is needed, and the local HTTPS/CDN responder uses port 8443.

## Installer menu

Run `./install-evejs-linux.sh` and choose:

1. **Install EveJS** into an existing empty folder.
2. **Update an existing installation** from a newer EveJS release ZIP. Your world, Market,
   settings and certificates stay in place (see *Updating* below).
3. **Undo an update or reseed**: restore program files and/or data from a backup.
4. **Reseed the Market** of an existing installation with one of the install-time seed
   profiles (v1, v2 or v3).
5. **Prepare/verify runtime compatibility**: re-checks an installation and refreshes the
   launcher, client pre-check and `tools/update-evejs-linux.sh` to this Setup's versions.
6. **List or delete backups.**
7. **Use a shared Proton prefix** for an existing installation (see *Proton prefix* below).

Non-interactive use: `./install-evejs-linux.sh --help`.

## Proton prefix (EVE settings)

The EVE client keeps its graphics and UI settings inside the Proton prefix. Earlier versions
made a new prefix for every installation, so those settings started from scratch each time.

From v1.17 a fresh installation looks in `~/.local/share/evejs-linux/prefixes/` for a prefix
an EveJS launcher already used (Proton files, Z: drive and EVE settings present) and offers to
reuse it, newest EVE use first; the prefix you reuse is remembered in
`~/.local/share/evejs-linux/shared-prefix` and offered first next time. Choose
**Create a new prefix** for a clean one. Prefixes last used by a newer Proton than the one
you picked are not offered, because Proton would rebuild them.

Menu option 7 points an existing installation at a shared prefix; its old prefix stays on disk
until you delete it. Non-interactive: `--prefix reuse`, `--prefix new` or `--prefix <path>`.

## Updating

Every installation gets `tools/update-evejs-linux.sh`, the Linux twin of EveJS's own
`UpdateEveJS.bat`. Stop the launcher session (server, Market and clients) first.

```bash
tools/update-evejs-linux.sh update ~/Downloads/EveJS-<version>.zip --preview   # what would change
tools/update-evejs-linux.sh update ~/Downloads/EveJS-<version>.zip             # do it
tools/update-evejs-linux.sh list                                               # backups
tools/update-evejs-linux.sh restore                                            # undo the newest update
tools/update-evejs-linux.sh restore <backup> --program-only | --data-only
tools/update-evejs-linux.sh prune --keep 3
tools/update-evejs-linux.sh reseed-market v1|v2|v3
```

What an update does:

- Follows the release's own `tools/Updater/update-policy.json`, exactly like the Windows
  updater: settings files (`config/*.json`) are kept, `_local/`, the Market data, compiled
  Market programs, `node_modules` and certificates are never touched, and files that are not
  part of a release (your own tools, `mods/`, the Linux launcher) are left alone.
- Moves the old program files to `_backup/pre-update-<version>-<stamp>/` and records what it
  installed in `.evejs-installed-files.json`.
- **Also copies your data** (world database, Market database, settings, certificates,
  launcher config) into the same backup folder, verified byte for byte, because an update can
  change the world on its first start. `restore` can therefore undo an update completely,
  and keeps the state it replaces as a `pre-restore-…` backup, so a restore can be undone too.
- Reinstalls Node packages only when the release changed them, runs the release's one-time
  sovereignty step, records the official release for the launcher, and prepares a Market
  server program for the new Market source (built with Cargo, or taken from the SteamOS bundle).

Reseeding replaces every Market order (including your own and what they hold); hangars,
wallets and the rest of the world are untouched. The previous Market is backed up to
`_backup/pre-reseed-<stamp>/` and `restore` puts it back.

## Which EveJS releases the launcher accepts

The launcher runs stock EveJS whose Market server source is either in this Setup's audited
list (`compatibility/market-stock-v*.json`: 0.12.9 and 261018.1) or exactly what the official
release ZIP recorded by the installer or updater shipped (`_local/evejs-linux-setup/release.json`).
Living Universe, X-Patch and unknown source states fail closed.

## SteamOS bundle for a new EveJS release

The bundle is bound to the parts of a release it was built from (lockfile contents and each
Rust program's source), so it keeps working across releases that leave them unchanged. When a
release changes them, rebuild it on a Linux PC with podman or docker:

```bash
tools/build-steamos-payload.sh ~/Downloads/EveJS-<version>.zip
```

## Fresh installation

Choose **1**, select the EveJS release ZIP, a separate copied EVE client (build named by the
release, currently 3396210) with its resource cache, your normal official Steam Proton, and a
new empty EveJS destination. Market v1 remains the recommended first profile.
