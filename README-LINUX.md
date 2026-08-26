# EveJS v0.12.6 — Universal Linux Setup v1.9.1
## Installer/Updater + Package-Owned Living Universe Handoff

v1.9.1 extends the v1.8.1 Installer/Updater and v1.9 runtime Launcher so one launcher
architecture can safely operate three factual, mutually-exclusive installation
states:

1. **Stock EveJS v0.12.6**
2. **Loader Living Universe** at `mods/x-eve-living-universe/loader.js`
3. **X-Patch Living Universe 0.3.2**, identified and verified through
   `_local/x-eve-patch/install.json`

The loader Living Universe and X-Patch Living Universe are not supported
simultaneously. If both are active, startup fails closed before Market/Game/EVE
are launched.

## Protected-file / installation-authority rule

The **runtime compatibility layer** does not manually patch, repair, install,
uninstall, or rewrite EveJS source, loader-LU files, or X-Patch-LU files.

When the user explicitly chooses to install a Living Universe package from the
Installer, v1.9.1 delegates the installation to that selected package's **own
supplied installer** instead of recreating its installation logic:

- Mod Living Universe: its own `install-linux.sh <EVEJS_ROOT>`;
- X-Patch Living Universe: its own `xeve-patch verify/install/verify` sequence.

Therefore any files intentionally added/replaced by a Living Universe package
are changed by that package's own installer under its own validation, backup and
rollback contract—not by a new Universal-Setup overlay method.

The source-matched Rust Market executable is built from a temporary **external
copy** of the installed, verified Market Rust build inputs. Cargo's target
output and the accepted runtime binary are stored outside the EveJS tree:

```text
<evejs-parent>/.evejs-linux-runtime/<path-id>/market-builds/<source-digest>/
```

Normal runtime data remains normal runtime data: when EveJS/Living Universe is
actually played, databases/logs can naturally change. X-Patch's own topology
synchronization is invoked before its Market daemon because it is part of the
provided X-Patch startup contract; it acts on the runtime Market database, not
on EveJS/X-Patch source files.

## Why this exists

Living Universe 0.3.2's Node economy calls two X-Patch Market RPC methods:

```text
GetSeedStocks
AdjustSeedStocks
```

Stock EveJS v0.12.6's Rust Market daemon does not implement those methods.
X-Patch LU 0.3.2 does implement them in its patched Rust source.

A Linux installation can therefore fail if X-Patch source is present but an
older stock/stale Market executable is launched. v1.9 prevents that
source/binary skew.

## Audited Market source identities

The compatibility authority hashes the same Rust build-input class used by the
provided X-Patch Windows freshness orchestration: Market `Cargo.toml`,
`Cargo.lock`, Rust source, and crate Cargo/Rust files.

### Stock EveJS v0.12.6 / loader-LU

```text
E9164F59BA3140A4C1AA792D9BF82896D10ED266A43D1BCD062166F77DA9025A
```

### X-Patch Living Universe 0.3.2

```text
CA8F89FB0175B9207014E9EEC1461376C195DE0A9F34E7E4490B92C230435A8C
```

Unknown or modified Market source is refused instead of guessed.

## X-Patch installed-state verification

X-Patch mode requires all of the following:

- EveJS exactly `0.12.6`;
- X-Patch plugin exactly `0.3.2`;
- X-Patch state declares EveJS `0.12.6`;
- exactly 210 tracked installed files;
- every tracked file exists with its recorded size and SHA-256;
- the X-Patch Market Rust build-input manifest matches exactly;
- loader-LU is not simultaneously active.

A partial X-Patch signature without a valid install record is rejected.

## Loader-LU verification

The known loader Living Universe is detected by the established path:

```text
mods/x-eve-living-universe/loader.js
```

Its complete mod directory receives a read-only tree digest during compatibility
classification/preparation. Because this loader implementation preserves clean
EveJS Market Rust source, it uses the same stock Market build profile as stock
EveJS.

Other normal `mods/*/loader.js` modules continue to be supplied through Node
`--require` as before.

## Launcher startup contract

The Launcher remains runtime-only. It has **no Update option and no LU installer**.

Before Market starts it:

1. detects stock / loader-LU / X-Patch-LU;
2. rejects loader-LU + X-Patch-LU together;
3. verifies the installed Market source identity;
4. requires a cached Market executable built for that exact source digest;
5. verifies the cached executable SHA-256;
6. starts that exact binary with the normal EveJS Market working directory;
7. verifies the listener PID is executing that exact binary;
8. retains the existing HTTP/RPC ownership checks.

For X-Patch LU only, it additionally:

1. runs the installed X-Patch `syncLivingEconomyMarketTopology.js` against the
   runtime Market DB;
2. starts the exact source-matched external Market binary;
3. probes `GetSeedStocks` with an empty key set;
4. probes `AdjustSeedStocks` with an empty adjustment set;
5. requires both RPC responses to return `ok=true`;
6. only then starts the EveJS Node server.

This directly blocks the observed failure mode where Market appears healthy but
is missing the Living Economy RPC surface.

## Installer opening menu

GUI and console fallback expose the same Setup functions:

```text
[1] Install EveJS v0.12.6
[2] Update an existing EveJS installation
[3] Install/prepare Living Universe or verify runtime compatibility
[4] Restore an upgrade backup
[5] Manage upgrade backups
[6] Exit
```

### Install/prepare or verify compatibility

Use option 3 when:

- attaching v1.9.1 to an existing Universal Linux v0.12.6 installation;
- installing one Living Universe variant onto an existing stock v0.12.6 tree using that package's own installer;
- X-Patch LU 0.3.2 has already been applied with its own patch manager;
- an earlier runtime cache is missing;
- the Launcher explicitly reports that compatibility preparation is required.

The operation requires EveJS/Market/EVE to be stopped. If the selected tree is
stock, the Installer can first offer Mod LU or X-Patch LU and invoke that
package's own installer. Afterward it validates the resulting tree, builds from
an external source copy, runs Market `doctor`, verifies that compatibility
preparation did not change the recognized LU/source identity, and installs only
the Universal Setup's own Launcher/helper files.

The compatibility layer never calls X-Patch `repair`/`uninstall`; an explicit
X-Patch install choice calls the selected package's own `xeve-patch install`.

## Fresh install

A fresh stock EveJS v0.12.6 installation still follows the established Linux
flow (client build 3396210, Proton, certificates, static DB, selected Market
profile, SteamOS minimal-native guard). At completion, the just-built stock
Market executable is also recorded into the external verified runtime cache.

If the loader-LU is installed later, it can use that same stock Market cache.

If X-Patch LU is chosen at the fresh-install end prompt, its own `xeve-patch`
installer applies the package and v1.9.1 immediately prepares the corresponding
external X-Patch Market runtime. If X-Patch is applied separately later, the
Launcher blocks until Installer option 3 prepares that runtime.

## EveJS update workflow

Installer-owned update/backup behavior remains separate from the Launcher.
The audited v0.12.5 -> v0.12.6 updater still preserves loader mods and player
state. After a successful target commit, it prepares the source-matched external
Market runtime before accepting the update.

The updater does not attempt to reinterpret or rewrite an X-Patch source
overlay as part of an EveJS version transition.

## SteamOS

The v1.8.1 native safeguards remain:

- no `steamos-unminimize --dev`;
- no broad `base-devel` repair;
- no `pacman -Sy` / partial upgrade;
- one proven missing SteamOS package at a time;
- installed package version must exactly equal repository version;
- Pacman dry-run must contain exactly that one package.

The Rustup Cargo proxy-path fix also remains: Cargo paths are preserved without
symlink dereference, preventing `cargo` from accidentally becoming `rustup`.

## Usage

```bash
tar -xzf EveJS-v0.12.6-Universal-Linux-Setup-v1.9.1-LU-Install-Flow-Hotfix.tar.gz
cd EveJS-v0.12.6-Universal-Linux-Setup-v1.9.1-LU-Install-Flow-Hotfix
./install-evejs-linux.sh
```

## Validation status

```text
STOCK SOURCE CLASSIFICATION                         PASS
LOADER-LU CLASSIFICATION                            PASS
X-PATCH LU 0.3.2 / 210-FILE VERIFICATION            PASS
LOADER-LU + X-PATCH CONFLICT REJECTION              PASS
PARTIAL/UNKNOWN X-PATCH FAIL-CLOSED                 PASS
STOCK/X-PATCH MARKET SOURCE MANIFESTS               PASS
EXTERNAL-COPY BUILD ORCHESTRATION UNIT TEST          PASS
LOADER-LU TREE CONSERVATION                         PASS
X-PATCH 210-FILE CONSERVATION                       PASS
MARKET RUST SOURCE CONSERVATION                     PASS
GETSEEDSTOCKS / ADJUSTSEEDSTOCKS RPC FIXTURE        PASS
UNSUPPORTED X-PATCH RPC REJECTION                   PASS
LAUNCHER HAS NO UPDATE/INSTALL-LU FUNCTION           PASS
MOD-LU OWN install-linux.sh METHOD TEST               PASS
X-PATCH OWN verify/install/verify METHOD TEST         PASS
UNIVERSAL SETUP MANUAL LU PAYLOAD OVERLAY             ABSENT
REAL NATIVE STOCK/X-PATCH RUST BUILD                 LIVE TEST PENDING
X-PATCH RPC LIVE STARTUP PROBE                       LIVE TEST PENDING
```

## v1.9.1 live-install hotfix

The first fresh v1.9 live run completed EveJS dependencies, certificates,
database creation, Market seeding/build/doctor and copied-client preparation,
then stopped while installing the portable Launcher with:

```text
line 1745: repo: unbound variable
```

The cause was Bash `set -u` combined with one declaration that referenced
`$repo` in the same `local` command that first assigned `repo`:

```bash
local repo="$1" tools="$repo/.evejs-linux-tools"
```

v1.9.1 splits those assignments, so the already-completed clean installation
can continue through Launcher/runtime-tool installation.

### Optional Living Universe handoff after a fresh install

After the clean v0.12.6 setup and stock runtime compatibility preparation pass,
the Installer asks whether to:

```text
1. Keep clean EveJS v0.12.6
2. Install Mod Living Universe
3. Install X-Patch Living Universe
```

The Universal Setup does **not invent a replacement installation procedure**.

For Mod Living Universe, the user selects that mod's release ZIP and v1.9.1
executes the package's own documented Linux entry point:

```text
./install-linux.sh <EVEJS_ROOT>
```

For X-Patch Living Universe 0.3.2, the user selects that release ZIP and v1.9.1
uses the supplied `xeve-patch` CLI itself:

```text
xeve-patch verify --integrity
xeve-patch verify <EVEJS_ROOT>
xeve-patch install <EVEJS_ROOT> --yes
xeve-patch verify <EVEJS_ROOT>
```

The supplied X-Patch Linux ZIP stores `xeve-patch` without a Unix execute bit.
The setup does not chmod/rewrite the package file. It runs an exact-byte
temporary executable copy outside the package tree and supplies the CLI's own
documented `--data-root` path back to the package's `patches/` directory.

The two Living Universe forms remain mutually exclusive and startup fails
closed if both are present.

After either package's own installer succeeds, v1.9.1 only performs the
separate Launcher compatibility step: detect the resulting installed state,
prepare/verify the matching external Market binary, and leave the selected
Living Universe package's source authority to that package.

The same package-owned installation handoff is available from Installer option
3 for an existing stock Universal-Linux EveJS v0.12.6 tree.

### v1.9.1 package-owned-method validation

The exact current Mod Living Universe Candidate 5D.3C.2 package was exercised on
a disposable clean v0.12.6 tree using **its own** `install-linux.sh`; its 41/41
self-check and 1,465-file clean-EveJS audit passed. Its own `uninstall-linux.sh`
then returned the disposable tree to a clean audit.

The exact supplied X-Patch 0.3.2 Linux package was likewise exercised with its
own CLI: release integrity 210/210, clean baseline 210/210, install, installed
state 210/210, and uninstall/clean verification all passed.

This validates the delegation contract; a full end-to-end user-machine fresh
install followed by the final prompt remains the live acceptance gate.
