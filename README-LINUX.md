# EveJS v0.12.5 — Universal Linux Installer v1.5
## SteamOS Toolchain Hardening

v1.5 is based on the live-tested v0.12.5 Linux port and retains the v1.4
process-ownership launcher architecture.

The new work is specifically aimed at SteamOS and other Linux systems where a
compiler executable exists but the native development headers/toolchain are
incomplete.

## Problem addressed

A SteamOS tester reached the v0.12.5 `market-seed` Rust build and Cargo failed
inside `libsqlite3-sys v0.28.0` while compiling bundled SQLite:

```text
fatal error: stdio.h: No such file or directory
```

`tools/market-seed/Cargo.toml` enables rusqlite's `bundled` feature, so SQLite's
C source must be compiled on the host. Testing only whether `cc` exists is not
enough.

## v1.5 behavior

Before asking the researcher to spend time on the full EveJS install, v1.5:

1. checks that required host commands exist;
2. compiles and runs a tiny C test using standard libc headers;
3. compiles and runs a tiny C++ test;
4. only then continues to Node/Rust/EveJS installation.

This catches missing `stdio.h`, broken libc headers, missing C++ headers,
incomplete compiler sysroots, and basic native-linker failures early.

## SteamOS-specific recovery

If the native probe fails and SteamOS is detected, the installer explains the
problem and offers to invoke SteamOS's own development restoration path:

```text
sudo steamos-readonly disable
sudo steamos-devmode enable       (when the command exists)
sudo steamos-unminimize --dev
```

Afterwards it repeats the real C/C++ compile-and-run probes. EveJS installation
does not continue until they pass.

The installer intentionally does **not** blindly reinstall Arch `glibc` from
generic repositories on SteamOS.

A SteamOS update can replace changes to the system image, so a future install
may need the development restoration again.

## Diagnostics

A successful installation records:

```text
<EveJS>/LINUX-HOST-TOOLCHAIN.txt
```

with the OS release, compiler versions, root filesystem space, key header state,
and SteamOS/pacman information where available.

## Runtime launcher

The v1.4 runtime safety architecture is retained:

- Market 40110/40111 process ownership;
- EveJS 443/5222/26000/26001/26002/26003/26400 process ownership;
- XMPP/Gateway TLS validation;
- stale EveJS process-tree cleanup;
- Proton CA handling;
- retry-safe launcher/restart behavior;
- `mods/*/loader.js` support.

No SteamOS-specific runtime workaround is required once the installation has
successfully completed.

## Installation

```bash
tar -xzf EveJS-v0.12.5-Universal-Linux-Installer-v1.5-SteamOS-Hardening.tar.gz
cd EveJS-v0.12.5-Universal-Linux-Installer-v1.5-SteamOS-Hardening
./install-evejs-linux.sh
```

On SteamOS, if development files are missing, follow the installer prompt.

Market `v1` remains the recommended first-test profile.
