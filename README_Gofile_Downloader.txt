GOFILE DOWNLOADER - DESKTOP EDITION
==================================

WINDOWS: EASIEST WAY
1. Extract the zip file into one folder.
2. Double-click Start_Gofile_Downloader.bat.
3. Paste any Gofile folder URL or ID (example: kaHyq5zp).
   For several folders, paste one link per line.
4. Choose All files, Videos only, or Photos only.
5. Select the download folder (or keep the default).
6. Click "Preview files" to inspect the folder contents, or "Download" to start.
7. Watch file-count progress and click "Open folder" to see saved files.

First run: The launcher checks for Python and requests. If requests is
missing, it asks before installing it. Python 3.10+ is required.

MAC / LINUX
Install Python 3.10+ and requests:
    python3 -m pip install requests
Then run:
    python3 gofile_bulk.py
On Linux, Tkinter may be installed separately as python3-tk.

TIPS
- Preview does NOT download files or create directories.
- Paste multiple links in the same input, separated by line breaks or commas.
- Use Cancel to stop; incomplete .part downloads can resume next time.
- Completed files are skipped if their size/checksum can be verified.
- A changed file on disk will NOT be silently overwritten.
- Expand Advanced options to set concurrent downloads or enter a folder
  password or an authorized Gofile account token.
- The app checks MIME types and file extensions for photo/video filtering.
  It does not scan the actual file contents.
- The progress bar counts processed files, not bytes transferred.
- Gofile's guest/API access rules may change, preventing listing of some
  folders. If you encounter authorization failures, use an authorized token.

COMMAND-LINE (unchanged)
    py gofile_bulk.py kaHyq5zp --media videos -o "D:\Videos"
    py gofile_bulk.py https://gofile.io/d/kaHyq5zp --dry-run
    py gofile_bulk.py --help

REQUIREMENTS
    Python >= 3.10, requests
    Tkinter for desktop mode (bundled with most Windows Python installers)

OFFLINE VERIFICATION
    python -m unittest -q test_gofile_bulk
This command checks local logic only. It does not access Gofile.
