# Gofile Bulk Downloader

A Python-based Gofile bulk downloader with a desktop interface for Windows, macOS, and Linux. Download complete shared folders or filter for videos and photos, without manually opening each file.

> **Independent project:** Not affiliated with Gofile. Download only content you are authorized to access. Gofile API access and authentication requirements may change.

## Features

- Desktop GUI with a Windows double-click launcher
- Accepts a Gofile folder ID or full share URL
- Multiple folders in one run (separate URLs/IDs with commas or new lines)
- **All files**, **Videos only**, and **Photos only** modes
- Recursive downloads from nested folders
- Preview matching files before downloading
- Concurrent transfers (1–8 workers) with retries
- Resumable partial downloads using `.part` files
- Skip verified completed files; do not silently overwrite changed files
- Download progress, activity log, Cancel, and Open folder controls
- Optional folder password or authorized Gofile account token
- Command-line mode for automation

## Requirements

- Python **3.10 or newer**
- [`requests`](https://pypi.org/project/requests/)
- Tkinter for desktop mode (typically included with Windows Python installers; some Linux distributions require `python3-tk`)

## Windows: Quick start

1. Download or clone this repository and keep its files in one folder.
2. Double-click **`Start_Gofile_Downloader.bat`**.
3. Paste a working Gofile link, such as `https://gofile.io/d/YOUR_FOLDER_ID`, or enter just `YOUR_FOLDER_ID`.
4. Choose **All files**, **Videos only**, or **Photos only**.
5. Select the download destination or keep the default.
6. Click **Preview files** to inspect the match list, or **Download** to start.
7. Follow the progress display, then click **Open folder** to view saved files.

The launcher checks for Python and `requests`. If `requests` is missing, it asks before installing it. Python must already be installed.

## macOS and Linux

Install the dependency and launch the desktop interface:

```bash
python3 -m pip install -r requirements_gofile.txt
python3 gofile_bulk.py
```

On Linux, install Tkinter separately if required by your distribution (for example, `sudo apt install python3-tk` on Debian/Ubuntu).

## Command-line usage

```powershell
# Download only videos
py gofile_bulk.py YOUR_FOLDER_ID --media videos -o "D:\Videos"

# Preview a folder without downloading
py gofile_bulk.py https://gofile.io/d/YOUR_FOLDER_ID --dry-run

# Download all files with four concurrent workers
py gofile_bulk.py YOUR_FOLDER_ID --media all --workers 4 -o "D:\Downloads"

# Show all available arguments
py gofile_bulk.py --help
```

On macOS and Linux, replace `py` with `python3` and use a destination path appropriate for your system. If you run the script without arguments, the desktop interface opens where Tkinter is available.

### Optional authentication

Expand **Advanced options** in the desktop interface to enter a folder password or your authorized Gofile account token. In command-line mode, use the `GOFILE_PASSWORD` and `GOFILE_TOKEN` environment variables.

Never commit tokens, passwords, `.env` files, or downloaded content to a public repository.

## Behavior and limitations

- **Preview** only lists files. It does not download files or create destination directories.
- You can paste multiple folders separated by new lines or commas.
- **Cancel** stops further work; a partially downloaded `.part` file may resume on the next run.
- Existing completed files are skipped when their size or checksum can be verified. Changed files are not silently overwritten.
- Video/photo selection uses file extensions and API-reported MIME types, **not** inspection of file content.
- The progress bar measures **processed files**, not bytes or transfer speed.
- Guest/API restrictions can prevent access to some folders, even if the link was previously accessible.

### Troubleshooting: `error-notFound` or HTTP 404

1. Click **Check first link in browser**, or open the share URL in your browser directly.
2. Check that the folder ID is correct and the link still exists.
3. If the folder opens only while signed in, use your own authorized Gofile account token under **Advanced options**.
4. If it does not open in the browser, ask the owner for a fresh share link.

A folder may be invalid, expired, deleted, or restricted. The downloader cannot recover a deleted folder or bypass access controls.

## Offline tests

```bash
python -m unittest -q test_gofile_bulk
```

This checks local downloader logic; it does **not** contact Gofile or validate live downloads.

## Project files

| File | Purpose |
| --- | --- |
| `gofile_bulk.py` | Downloader and desktop GUI |
| `Start_Gofile_Downloader.bat` | Windows launcher |
| `requirements_gofile.txt` | Python dependencies |
| `test_gofile_bulk.py` | Offline test suite |
| `README.md` | Installation and usage guide |
