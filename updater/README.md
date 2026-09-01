# app-updater (`update.exe`)

A tiny, generic, dependency-free Windows updater for **folder-based** apps
(Nuitka `--standalone`, PyInstaller onedir, etc.). Statically linked std-only
Rust → one ~300 KB `update.exe` with **no DLLs** and **no interpreter** (no
PowerShell/cmd), so it works on locked-down machines where a `.ps1` is blocked
by execution policy.

## What it does

```
update.exe <src_dir> <install_dir> <main_exe_name>
```

1. Waits for `<install_dir>\<main_exe_name>` to become writable (the app has exited).
2. Mirrors `<src_dir>` (the already-unpacked new build) over `<install_dir>`.
3. Prunes files the new build no longer ships — but never `unins000.exe`,
   `unins000.dat`, `update-log.txt`, or an `update\` staging folder.
4. Relaunches the app. It **always** relaunches at the end, even on failure.

Progress is written to `<install_dir>\update-log.txt`.

## How the app drives it

Because a program can't overwrite its own running exe, the app copies **just
this one file** to a temp folder and runs it from there, so it can replace the
entire install (including the installed `update.exe`) with no self-replace
problem:

```python
tmp = Path(tempfile.mkdtemp()) / "update.exe"
shutil.copy2(install_dir / "update.exe", tmp)
subprocess.Popen([str(tmp), str(src_dir), str(install_dir), "OreHoldWatcher.exe"],
                 creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                               | CREATE_BREAKAWAY_FROM_JOB)
# then quit the app so its exe unlocks
```

## Build

```
cargo build --release --manifest-path updater/Cargo.toml
# -> updater/target/release/update.exe
```

Ship `update.exe` inside the program folder (it ends up in the release zip and
the installer automatically).

## Reuse in another app (e.g. Eve-Strait)

Nothing here is app-specific — the app name, install path, and exe name all come
from argv. Copy this `updater/` folder into the other repo (or share the built
`update.exe`), build it the same way, drop `update.exe` in the program folder,
and have that app spawn it with its own exe name.

## License

MIT — Copyright (c) 2026 Jammy LLC. See [LICENSE](LICENSE).

This folder is licensed independently of the parent repository. Ore Hold Watcher
as a whole is GPLv3, but `updater/` is MIT-licensed Jammy LLC code that is reused
across projects, so it can be copied into other apps under MIT terms alone.
