<h1 align="center">
  Syntax
  <a href="https://github.com/Astraliscute/Syntax/releases/latest"><img src="https://img.shields.io/github/v/release/Astraliscute/Syntax?label=Latest%20Release" alt="Latest Release"></a>
</h1>

<h3 align="center">A Windows FFlag Manager</h3>

<p align="center">Fast Flags, Locked In. Built for Speed & Stability.</p>

<p align="center">
  <img src="https://img.shields.io/github/downloads/Astraliscute/Syntax/total?label=Downloads&color=brightgreen" alt="Downloads">
  <img src="https://img.shields.io/github/license/Astraliscute/Syntax" alt="Syntax License">
</p>

<p align="center">
  <img src="https://img.shields.io/github/repo-size/Astraliscute/Syntax?label=Repository%20Size" alt="Repository Size">
  <img src="https://img.shields.io/github/languages/code-size/Astraliscute/Syntax?label=Code%20Size" alt="Code Size">
  <img src="https://img.shields.io/github/languages/top/Astraliscute/Syntax?label=Top%20Language" alt="Top Language">
  <img src="https://img.shields.io/github/commit-activity/m/Astraliscute/Syntax?label=Monthly%20Commits" alt="Monthly Commits">
  <img src="https://img.shields.io/github/issues/Astraliscute/Syntax?label=Issues" alt="Issues">
</p>

<p align="center">
  <sub>Enjoying Syntax? <strong><a href="https://github.com/Astraliscute/Syntax">Give it a star</a></strong> and join the <a href="https://discord.gg/98HWke3zMg">Discord Server</a>.</sub>
</p>

<p align="center">
  <a href="https://github.com/Astraliscute/Syntax/releases/latest">Download</a> ·
  <a href="https://github.com/Astraliscute/Syntax/issues">Report an Issue</a>
</p>

<p align="center">
  <strong>Server:</strong> <a href="https://discord.gg/98HWke3zMg">Syntax Discord Server</a>
</p>

---

## Discord Server and Support

For support, announcements, Server FFlag presets, and discussions, visit the [Syntax Discord Server](https://discord.gg/98HWke3zMg).

## Table Of Contents

- [Offsets and Built-In Offset Dumper](#offsets-and-built-in-offset-dumper)
- [Build and Verify](#build-and-verify)
- [Issues](#issues)
- [Credits](#credits)

## Features

* **Automatic Memory Locking** — Syntax locks FFlags from memory automatically.
* **FFlag Browser** — Browse and inspect available FFlags directly inside Syntax.
* **Debloat FFlags** — Remove FFlags that were deleted from the offsets (Roblox file).
* **Server FFlags** — Share or upload FFlag presets via the Syntax Discord Server.

#### How to Run

Open `syntax.exe` on 64-bit Windows 10/11

For source users (64-bit Python 3.10+):

```powershell
py -3 -m pip install -r requirements.txt
py -3 syntax.py

```

---

> Want to upload an FFlag preset into Syntax? Join the [Syntax Discord Server](https://www.google.com/url?sa=E&source=gmail&q=https://discord.gg/98HWke3zMg).

---

### Offsets and Built-In Offset Dumper

* **Default Source (`offsets.theo`)** — Uses `offsets.theo` out of the box.
* **Local Offset Dumper** — Built-in local dumper by **Syscall** to dump offsets directly from your Roblox client.

#### How Offset Dumping Works

You can also dump offsets directly from your Roblox client so FFlag injection stays available.

1. Start Roblox.
2. Go to the **Settings** tab and click **Dump offsets**.
3. Wait for the saved count, or click **Cancel** to stop.
4. Select **Local dump** to use your dump, or select **theo (default)** to switch back.

Dumped offsets are saved to:

```text
%LOCALAPPDATA%\Syntax\offsets\

```

---

### Build and Verify

* **Windows Build & Checks** — Run `--check`, `--test`, and `--build` via `syntax.py`.

#### How to Build and Verify

On Windows:

```powershell
python syntax.py --check
python syntax.py --test
python syntax.py --build

```

On Linux (with Wine and a Windows Python installation):

```bash
./compile.sh

```

> On Linux, you need Wine to build.

## Issues

Found a bug or have a feature request?

* [Report a Bug](https://www.google.com/search?q=https://github.com/Astraliscute/Syntax/issues/new)
* [Request a Feature](https://www.google.com/search?q=https://github.com/Astraliscute/Syntax/issues/new)
* [View Existing Issues](https://www.google.com/url?sa=E&source=gmail&q=https://github.com/Astraliscute/Syntax/issues)

## Credits

* **Syscall** - Built-in local offset dumper.
* **Syntax** - Main app.
* **offp** -  UI, Protection, Anti-Crash, FFlag locking

 <p align="center">
<sub>Syntax by Astral - also offp helped much :)</sub>
 </p>