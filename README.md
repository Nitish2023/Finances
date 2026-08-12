# Finances — Local Expense Tracker

100% local, privacy-focused Android app that reads financial SMS on-device,
parses transactions (including credit card charges), and stores everything
in a local SQLite database. No network calls, no cloud sync.

## Files
- `main.py` — the full Kivy/KivyMD app
- `buildozer.spec` — Android build configuration
- `.github/workflows/build.yml` — builds a debug APK automatically on every
  push to `main` (and can be triggered manually from the Actions tab)

## Get the APK (no local setup needed)
1. Push this repo to GitHub (see commands below).
2. Go to the repo's **Actions** tab → the "Build APK" workflow run.
3. Once it finishes (15–30 min), open the run → **Artifacts** section →
   download `expense-tracker-apk`. Unzip it to get the `.apk`.
4. Transfer the `.apk` to your phone (email, Drive, USB, etc.) and tap it
   to install. You'll need to allow "Install unknown apps" for whichever
   app you used to open it — that's expected for a non-Play-Store APK.
5. Open the app, grant SMS permission when prompted, tap **Sync SMS**.

## Build locally instead (optional)
```bash
pip install buildozer cython
buildozer android debug
```
APK lands in `bin/`.
