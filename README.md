# 111477 Downloader

A small Windows GUI for browsing **a.111477.xyz** and sending whole seasons, folders or
single files to **Internet Download Manager (IDM)**.

The index itself is protected by Cloudflare, which is why pasting a file link straight
into IDM returns `403 Forbidden`. This app does the same thing your browser does: it
mints a temporary download link through the site's own proxy (`p.111477.xyz`) and hands
that link to IDM.

---

## Quick start

1. Double-click **`dist\111477-Downloader.exe`** (already built here) or **`run.bat`**
   if you prefer running from source.
   The source launcher installs `requests` + `patchright` if they are missing.
2. The first time you press **Send to IDM**, a Chrome window opens so Cloudflare can
   verify you. If a checkbox appears, click it. The window closes by itself when the
   check is done. This usually happens once per session (roughly every half hour).
3. Choose a folder (double-click), tick the episodes, then press:
   * **Send to IDM** – IDM starts immediately.
   * **Add to IDM queue** – safer for several episodes, nothing starts until you press
     *Start queue* in IDM.
4. If the yellow banner says IDM is using more than 1 connection, click **Set to 1**,
   then fully exit IDM (tray icon → Exit) and reopen it. See the limits below.

You can paste any index URL into the location bar and press **Go**, e.g.
`https://a.111477.xyz/kdrama/Welcome.to.Samdal-ri.S01.1080p.NF.WEB-DL.DDP2.0.H.264-MrHulk/`

---

## Proxy limits (important)

These limits come from the proxy service itself and apply to everyone:

| Limit | Value |
|---|---|
| Connections per file | **1** (set IDM's default connections to 1) |
| Simultaneous files | **2** per IP |
| Download quota | about **30 successful downloads per IP**, refilling ≈1 per 10 min (up to 50) |
| Link lifetime | **24 hours** |
| Uses per link | 50 successful download sessions |
| Locked to | your current IP address |

The app mints fresh links right before sending them, and warns you about the connection
limit. It does **not** bypass the quota.

If you see **"id busy"**, a previous session on that link is still open. The proxy site
offers a one-time *Unlock & Retry* (CAPTCHA) for that link; otherwise wait up to 30 minutes.

---

## Using the browser

* **Section box** – jump to `Root`, `asiandrama`, `kdrama`, `misc`, `movies`, `tvs`.
* **Path bar** – paste a folder URL or type a path, then press **Go**.
* **Filter** – type part of a name; `Esc` clears it.
* **Ticks** – click the box, press `Space`, or double-click the row.
* **Folders** – tick a series folder and press *Send to IDM*: the app scans it and
  queues every file inside (limited to 400 folders per action).
* **Selection is remembered** when you move between folders.
* **Right-click** a row for copy / send shortcuts.
* **Copy links** – mint worker links and put them on the clipboard (works with wget,
  curl, or another download manager).
* **Copy index URLs** – the original `a.111477.xyz` links (not usable without a browser).
* **Export…** – save the minted links to a `.txt` file.

Shortcuts: `F5` refresh · `Backspace` up · `Ctrl+A` select all · `Space` toggle.

---

## Settings

* Download folder (default `%USERPROFILE%\Downloads\111477`).
* Keep series/movie folder structure.
* Skip files that already exist.
* Only media/subtitle files when expanding folders.
* IDM path (auto-detected).
* Default action: start immediately or add to the queue.
* **IDM connections** – one-click set to 1, plus undo.
* **Verify proxy session now** – opens Chrome to refresh the Cloudflare clearance.
* **Reset browser profile** – if the verification gets stuck.
* Clear the index cache / open the data folder.

Settings, cache, logs and the verification browser profile live in:

```
%LOCALAPPDATA%\111477 Downloader\
```

(`session.json` contains the Cloudflare cookie – treat it as private.)

---

## Troubleshooting

**IDM says `403 Forbidden` (HTTP/1.1 403).**
You pasted an `a.111477.xyz` link. Those links are Cloudflare-protected and only work in a
browser. Use this app so IDM receives a `*.workers.dev/d/...` link instead.

**IDM stalls, says "id busy", or only downloads part of a file.**
IDM is using multiple connections. Set it to 1 (banner button or Settings), restart IDM,
and retry. Do not run more than 2 files at once.

**"Cloudflare verification did not complete in time."**
Run *Settings → Verify proxy session now* and click the checkbox in the Chrome window.
If it still fails, *Reset browser profile* and verify once more.

**"Could not start Chrome or Edge" / profile is locked.**
A previous verification window is still open. Close it (or reboot) and try again.

**The proxy is throttling (HTTP 429).**
The app retries with backoff. If it keeps happening, wait a few minutes.

**Index shows a rate-limit error (Cloudflare 1015).**
The index is limiting this IP. Wait a minute and press `F5`; cached folders still work.

**Nothing happens after "Add to IDM queue".**
Open IDM and start the queue (or use the app's *Open IDM* button).

---

## How it works

1. Reads the directory listing HTML (`data-name` / `data-url` / size).
2. Uses a chip of Chromium automation (`patchright`) to pass the proxy's Cloudflare
   check once, then reuses the `cf_clearance` cookie for plain HTTP.
3. `POST https://p.111477.xyz/bulk` with up to 10 index URLs at a time returns the
   signed `*.workers.dev/d/<hash>/<filename>` links.
4. Calls IDM non-interactively:

   ```
   IDMan.exe /n /d <link> /p <folder> /f <filename> [/a]
   ```

   (`/n` = silent, `/a` = add to queue without starting.)

No account, no scraping of the file contents, no bypassing of the proxy quota.

---

## Building a standalone EXE

### On your PC

Double-click **`build_exe.bat`** (needs Python 3.10+). It installs PyInstaller and creates:

```
dist\111477-Downloader.exe
```

### With GitHub Actions

`.github/workflows/build-windows.yml` builds the EXE on a Windows runner:

* **Actions → Build Windows EXE → Run workflow** – builds and uploads the EXE as a
  downloadable artifact.
* Push a tag such as `v1.0.0` – the EXE is also attached to the GitHub release.

If the folder is not a git repository yet:

```bash
git init
git add .
git commit -m "111477 downloader"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

The bundle includes Python, Tk and the small automation driver, but **not** a browser:
the app still uses the Chrome or Edge already installed on the machine. Because the EXE
is one-file, the first start takes a few seconds while it unpacks to `%TEMP%`.

It is not code-signed, so Windows SmartScreen may show “Windows protected your PC” –
choose *More info → Run anyway*.

---

## Files


```
app.py                 GUI
core/config.py         paths + settings
core/index_client.py   index listing client + cache
core/resolver.py       Cloudflare session + link minting
core/idm.py            IDM command line / registry helpers
core/store.py          sqlite cache
assets/                app icon
build_exe.bat          local EXE build
.github/workflows/     GitHub Actions EXE build
run.bat                normal launcher
run_debug.bat          launcher with a console (for error messages)
```

> Use this only for content you are allowed to download, and respect the site's quotas.
