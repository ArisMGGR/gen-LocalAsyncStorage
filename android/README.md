# Async Storage DOS — Android client

A native Android app (Java) that connects to the same Async Storage DOS server
as the desktop client, with the same on-demand model: browse the server's live
file list and download exactly the files you want.

## Features (parity with the desktop app)

- **Folder browser** (polled every ~4s): shows the folder you're in — sub-folders
  (📁, tap to open) and files. **⬆ Up** or the system **Back** button goes out; the
  current location is shown above the list.
- **Download** — tap a file's **⬇ Download**, use a folder's ⋮ → **Download
  folder**, or select several files/folders and use **⋮ → Download selected**. A
  file lands straight in your download folder; a folder lands in it with its own
  structure (empty sub-folders included). Nothing is tracked afterwards — the
  download folder is only looked at right then, to catch a name that's taken:
  - a **folder** that already exists there is not downloaded (a popup says so);
  - for **files** that already exist there you're asked once, for all of them:
    **Overwrite**, **Keep both** (`name (1).ext`) or **Skip**.
- **Search** — the box at the top filters to every file whose path matches, across
  all folders (each result shows its full path); the per-file actions, Move, and
  Delete work on results. Clear the box to return to the folder view.
- **Organise files:**
  - **Select multiple** — tap files to select them (they highlight); Download,
    Move and Delete then act on every selected item at once.
  - **New folder** — create a folder inside the one you're viewing (empty folders
    persist on the server and sync to your other devices).
  - **Drag and drop** — **long-press a file or folder to pick it up, then drop it
    on a folder** (or the **..** row to move it up a level). If you've selected
    several items first, dragging any one of them drops the **whole selection**.
  - **Move** (alternative) — select files and/or folders and use **⋮ → Move to…**
    to pick or type a destination folder; folders nest under it keeping their name.
- **Delete** — the red button (or a folder's ⋮ → **Delete folder**, or dragging
  rows onto the Trash button) moves the selection to **Trash** on the server;
  files you've downloaded are never touched. Nothing in Trash is overwritten — a
  second `a.txt` goes in as `a (1).txt`, a second `Album` as `Album (1)`.
- **Trash** has its own **🗑 Trash (n)** button at the top, next to the search box,
  instead of appearing as a folder. Tap it to open Trash, and **Back to files**
  (or the system Back button) to return. Inside Trash: **Restore** (back where it
  came from, or as `name (1).ext` if that spot is taken), **Delete forever**, and
  **Empty Trash**. Search outside Trash skips trashed files; inside, it searches
  only them. Things can't be moved into Trash — only deleted. The top-level name
  `Trash` is reserved, ignoring case: a top-level `trash` is refused, and one
  left from before must be moved into another folder before anything is deleted.
- **Add files** — pick files with the system document picker and upload them
  **into the folder you're viewing**, after a confirmation prompt. A name that's
  already used there is not uploaded (a popup lists them); nothing can be uploaded
  into Trash.
- **Tasks / Queue** — progress per running job with **Stop** (stopping a folder
  delete puts back what already reached Trash), and **✕** on waiting jobs. Asking
  for something already queued does nothing.
- sha256 verification on every transfer, and a **Settings** screen for the server
  URL + API key.
- **Background transfers:** uploads and downloads run in a foreground service, so
  they keep going if you leave the app, with an **ongoing notification** showing
  progress and speed. The app asks for notification permission on first launch
  (Android 13+); transfers still run if you decline, just without the notice.

**Where downloads go:** on first use the app asks you to **pick a folder** (via
the Android folder picker). Downloads are saved only into that folder — you can
browse it in the Files app, and it isn't published to shared media. The choice is
remembered across restarts, and no storage permissions are required. Pick or
create somewhere you'll find easily, e.g. a folder under **Documents**. You can
re-pick anytime from the **Open folder** button when none is set.

## Build & run

You need **Android Studio** (Narwhal 3 Feature Drop, 2025.1.3, or newer — the
oldest that supports the project's Android Gradle plugin 8.13) with an Android
SDK.

1. In Android Studio: **Open** → select this `android/` folder. It will provision
   the Gradle wrapper and sync automatically.
2. Connect a phone (USB debugging on) or start an emulator, then press **Run**.

Command line alternative (with a local Android SDK and a system Gradle to bootstrap
the wrapper):

```bash
cd android
gradle wrapper          # one-time: generates ./gradlew and the wrapper jar
./gradlew assembleDebug  # builds app/build/outputs/apk/debug/app-debug.apk
```

> The Gradle wrapper **jar** is a binary and isn't committed here; Android Studio
> (or `gradle wrapper`) generates it on first use.

## Configure

On first launch the app opens **Settings**. Enter:

- **Server URL** — e.g. `http://192.168.1.50:8000` (the server's LAN or Tailscale
  address). The server must be started bound to that address, e.g.
  `STORAGE_HOST=192.168.1.50 python -m server`.
- **API key** — the exact same `STORAGE_API_KEY` the server uses.

The phone must be able to reach the server — on the same Wi-Fi/LAN, or with
Tailscale installed and signed into the same account. The app allows cleartext
HTTP because the server is plain HTTP over that trusted network.

## Notes

- No third-party libraries: networking uses `HttpURLConnection` and JSON uses the
  built-in `org.json`.
- "Open folder" is best-effort — Android has no universal open-a-folder intent, so
  it attempts to open a file manager and otherwise shows the folder path.
