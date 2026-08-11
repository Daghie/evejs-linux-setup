# EveJS v0.12.5 — Universal Linux Installer
## v1.4 Port Candidate 1

This is the first v0.12.5 port built directly from the **live-accepted
v0.12.4.1 Universal Linux Installer v1.4**.

It is intentionally a candidate until a clean v0.12.5 installation, startup,
login, runtime, restart, and log review pass on Linux.

### Preserved v1.4 acceptance architecture

The installer/launcher retains the full v1.4 boundary:

- Steam's installed official Proton discovery;
- isolated Proton prefix per EveJS installation;
- graphical Zenity/KDialog wizard with terminal fallback;
- wrong interactive file/folder selections reopen the chooser;
- user must select an existing empty destination root;
- installer never creates the root installation folder;
- standalone stale-process cleanup tool installed with EveJS;
- stale cleanup resolves edge PIDs to the full EveJS process-tree root;
- Market starts before the game server;
- Market ports 40110/40111 must belong to the current Market process tree;
- EveJS ports 443, 5222, 26000, 26001, 26002, 26003 and 26400 must belong
  exclusively to the current Game/EveJS tree;
- gateway health and TLS checks;
- XMPP TLS verification;
- XMPP connect host forced to 127.0.0.1 for Proton;
- stale EvEJS Local CA cleanup in EVE client cacert.pem bundles;
- Wine-visible `Z:\...` CA path;
- removable `mods/*/loader.js` loading through Node `--require`.

### v0.12.5-specific changes

**Node.js:** v0.12.5 names Node 24 LTS as the native target. Candidate 1
prefers Node 24 and can install a private current Node 24.x runtime. The release
audit documents Node 20 and 22-26 as compatible with the current lock, while
Node 21 is excluded.

**Archive layout:** the uploaded v0.12.5 ZIP contains a top-level `v0.12.5/`
directory. The installer now discovers the real repository root safely instead
of assuming files are at ZIP root.

**Market:** v0.12.5 adds three supported seed strategies:

- `v1` — synthetic Jita + New Caldari. Offline/deterministic. **Recommended for
  the first Linux acceptance run.**
- `v2` — current Tranquility snapshot, using the shipped v2 local config.
- `v3` — cost-priced Jita 4-4 index over the authentic TQ NPC catalog. The
  installer runs doctor, explicitly caches the snapshot, runs the pump audit,
  then builds.

### Installation

Keep these three scripts together:

```text
install-evejs-linux.sh
evejs-linux-launcher.sh
evejs-v0.12.5-stale-processes.sh
```

Then:

```bash
chmod +x *.sh
./install-evejs-linux.sh
```

For the first acceptance test, select **market v1**.

Text mode:

```bash
./install-evejs-linux.sh --text
```

Optional scripted market choice:

```bash
./install-evejs-linux.sh --market v1
./install-evejs-linux.sh --market v2
./install-evejs-linux.sh --market v3
```

### Acceptance status

Current status:

```text
SOURCE AUDITED
STATIC VALIDATED
LIVE v0.12.5 ACCEPTANCE PENDING
```

Do not replace the protected v0.12.4.1 v1.4 baseline until this candidate passes
a real v0.12.5 installation and runtime log review.
