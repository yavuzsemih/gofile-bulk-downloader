#!/usr/bin/env python3
"""Download Gofile shared folders recursively, with optional guest authentication.

Dependencies: pip install requests
Credentials: GOFILE_TOKEN (optional), GOFILE_PASSWORD (optional).
The website token format is not a stable public API and may change.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import sys
import queue
import threading
import time
from urllib.parse import urljoin, urlparse

import requests

API = "https://api.gofile.io"
USER_AGENT = os.getenv("GOFILE_USER_AGENT", (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
))
LANGUAGE = "en-US"
# Public client-side constant reported by existing Gofile download clients.
# This is NOT an account credential; Gofile can change it without notice.
WT_SALT = os.getenv("GOFILE_WT_SALT", "12af056dacea0b")
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}
PAGE_SIZE = 1000

# Classification uses Gofile's reported MIME type when available, otherwise
# the filename extension. It does not inspect the downloaded file contents.
VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm", ".wmv", ".flv",
    ".mpg", ".mpeg", ".3gp", ".3g2", ".ts", ".mts", ".m2ts", ".ogv", ".vob",
})
PHOTO_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif",
    ".tif", ".tiff", ".avif", ".dng", ".cr2", ".nef", ".arw", ".raf",
    ".orf", ".rw2",
})


class DownloadError(Exception):
    pass


class DownloadCancelled(DownloadError):
    """Cancellation requested by the user; partial transfers remain resumable."""


def check_cancel(event: threading.Event | None) -> None:
    if event is not None and event.is_set():
        raise DownloadCancelled("Cancelled by user")


def pause(seconds: float, event: threading.Event | None = None) -> None:
    if event is not None:
        if event.wait(seconds):
            raise DownloadCancelled("Cancelled by user")
    else:
        time.sleep(seconds)


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def parse_shares(raw: str) -> list[tuple[str, str]]:
    """Parse one or more links/IDs with stable deduplication."""
    values = [x for x in re.split(r"[\s,;]+", raw.strip()) if x]
    if not values:
        raise DownloadError("Paste a Gofile folder URL or enter its folder ID.")
    results = []
    seen = set()
    for value in values:
        url, code = normalize_share(value)
        if code not in seen:
            results.append((url, code))
            seen.add(code)
    return results


def validate_gofile_url(url: str) -> str:
    """Reject redirects/links to non-Gofile hosts and insecure protocols."""
    u = urlparse(url)
    if (u.scheme != "https" or not u.hostname or u.username or u.password
            or not (u.hostname == "gofile.io" or u.hostname.endswith(".gofile.io"))):
        raise DownloadError(f"Rejected non-Gofile HTTPS URL: {url[:160]}")
    return url


def share_id(url: str) -> str:
    u = urlparse(url)
    if u.scheme != "https" or u.hostname not in ("gofile.io", "www.gofile.io"):
        raise DownloadError("Expected an HTTPS gofile.io/d/<code> share URL")
    match = re.fullmatch(r"/d/([A-Za-z0-9-]+)/*", u.path)
    if not match:
        raise DownloadError("Expected an HTTPS gofile.io/d/<code> share URL")
    return match.group(1)


def normalize_share(value: str) -> tuple[str, str]:
    """Accept a share ID or Gofile share URL; reject unrelated/unsafe URLs."""
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9-]+", value):
        code = value
    else:
        if value.startswith(("gofile.io/", "www.gofile.io/")):
            value = "https://" + value
        code = share_id(value)
    return f"https://gofile.io/d/{code}", code


def prompt_shares() -> list[str]:
    """Prompt for one or more share IDs or links, separated by spaces or commas."""
    try:
        raw = input("Gofile folder ID or URL (multiple: comma-separated): ").strip()
    except EOFError as exc:
        raise DownloadError("No share supplied. Enter a folder ID or pass it on the command line.") from exc
    if not raw:
        raise DownloadError("A Gofile folder ID or share URL is required.")
    return [part for part in re.split(r"[\s,]+", raw) if part]


def prompt_media() -> str:
    print("\nDownload selection:")
    print("  1. Videos only")
    print("  2. Photos only")
    print("  3. All files (default)")
    while True:
        try:
            choice = input("Select [1/2/3, Enter=3]: ").strip().lower()
        except EOFError as exc:
            raise DownloadError("No download selection. Use --media videos|photos|all.") from exc
        if choice in ("", "3", "all"):
            return "all"
        if choice in ("1", "videos", "video"):
            return "videos"
        if choice in ("2", "photos", "photo", "images", "image"):
            return "photos"
        print("Invalid selection. Choose 1, 2 or 3.")


def matches_media(obj: dict, filename: str, media: str) -> bool:
    """Filter only file downloads, never folder traversal."""
    if media == "all":
        return True
    if media not in ("videos", "photos"):
        raise ValueError(f"Unsupported media selection: {media}")
    mime = next((obj[key].lower().split(";", 1)[0].strip()
                 for key in ("mimetype", "mimeType", "mime", "contentType")
                 if isinstance(obj.get(key), str) and obj[key].strip()), "")
    if mime.startswith("video/"):
        return media == "videos"
    if mime.startswith("image/"):
        return media == "photos" and mime != "image/svg+xml"
    extension = Path(filename).suffix.lower()
    return extension in (VIDEO_EXTENSIONS if media == "videos" else PHOTO_EXTENSIONS)


def safe_name(name: str, fallback: str) -> str:
    """Use one safe cross-platform path segment, never a path from remote metadata."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip(" .")
    if name in ("", ".", ".."):
        name = f"item_{fallback[:8]}"
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?", name):
        name = "_" + name
    return name[:180].rstrip(" .") or f"item_{fallback[:8]}"


def unique_names(children: dict) -> list[tuple[str, dict, str]]:
    """Deterministically disambiguate case-insensitive and sanitized name collisions."""
    ordered = sorted(children.items(), key=lambda item: str(item[0]))
    labels = [(str(obj.get("id") or key), obj,
               safe_name(obj.get("name") or key, str(key)))
              for key, obj in ordered]
    counts = {}
    for _, _, name in labels:
        counts[name.casefold()] = counts.get(name.casefold(), 0) + 1
    results = []
    used = set()
    for cid, obj, name in labels:
        if counts[name.casefold()] > 1:
            stem, ext = os.path.splitext(name)
            name = f"{stem[:140]}__{cid.replace('-', '')[:12]}{ext}"
        if name.casefold() in used:
            raise DownloadError(f"Filename collision remains for {cid}")
        used.add(name.casefold())
        results.append((cid, obj, name))
    return results


def assert_safe_target(root: Path, path: Path) -> None:
    if not path.resolve().is_relative_to(root.resolve()):
        raise DownloadError(f"Target escapes output directory: {path}")
    # Do not follow symlinks created within the output tree.
    parent = path
    while parent != root:
        if parent.is_symlink():
            raise DownloadError(f"Refusing symlink in output path: {parent}")
        parent = parent.parent


def website_token(token: str, window_offset: int = 0) -> str:
    slot = int(time.time() // 14400) + window_offset
    raw = f"{USER_AGENT}::{LANGUAGE}::{token}::{slot}::{WT_SALT}"
    return hashlib.sha256(raw.encode()).hexdigest()


def retry_delay(attempt: int, retry_after: str | None = None) -> float:
    if retry_after and retry_after.isdecimal():
        return min(float(retry_after), 120)
    return min(3 * (2 ** attempt), 60)


class Gofile:
    def __init__(self, token: str | None = None, password: str | None = None):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT, "Origin": "https://gofile.io",
            "Referer": "https://gofile.io/", "Accept": "application/json",
        })
        self.token = token or self._guest_token()
        self.password_hash = hashlib.sha256(password.encode()).hexdigest() if password else None

    def _guest_token(self) -> str:
        for attempt in range(4):
            try:
                res = self.session.post(f"{API}/accounts", timeout=(15, 40), headers={
                    "X-Website-Token": website_token(""), "X-BL": LANGUAGE,
                })
                data = res.json()
                if data.get("status") == "ok" and data.get("data", {}).get("token"):
                    return data["data"]["token"]
                if res.status_code not in RETRYABLE and data.get("status") != "error-rateLimit":
                    raise DownloadError(f"Guest token creation failed: {data.get('status', res.status_code)}")
            except (requests.RequestException, ValueError) as exc:
                if attempt == 3:
                    raise DownloadError(f"Guest account request failed: {exc}") from exc
            if attempt < 3:
                time.sleep(retry_delay(attempt, res.headers.get("Retry-After") if 'res' in locals() else None))
        raise DownloadError("Guest token creation rate-limited; retry later or set GOFILE_TOKEN")

    def _get_page(self, folder_id: str, page: int) -> dict:
        params = {"page": page, "pageSize": PAGE_SIZE, "sortField": "createTime",
                  "sortDirection": -1, "contentFilter": ""}
        if self.password_hash:
            params["password"] = self.password_hash
        for attempt in range(5):
            try:
                # Try the prior 4h window on token rejection near a boundary.
                offset = -1 if attempt == 1 else 0
                res = self.session.get(f"{API}/contents/{folder_id}", params=params,
                                       timeout=(15, 50), headers={
                    "Authorization": f"Bearer {self.token}",
                    "X-Website-Token": website_token(self.token, offset),
                    "X-BL": LANGUAGE,
                })
                data = res.json()
                status = data.get("status")
                if status == "ok" and isinstance(data.get("data"), dict):
                    item = data["data"]
                    if item.get("canAccess") is False or (item.get("password") and
                            item.get("passwordStatus", "passwordOk") != "passwordOk"):
                        raise DownloadError(f"Access denied to folder {folder_id}: password/private folder")
                    return data
                if status == "error-notPremium":
                    if attempt == 0:
                        continue
                    raise DownloadError("Gofile rejected content listing (error-notPremium). "
                                        "The website token may have changed or a Premium account may be required. "
                                        "Try GOFILE_TOKEN with an authorized account; do not assume guest access.")
                if res.status_code not in RETRYABLE and status != "error-rateLimit":
                    raise DownloadError(f"Unable to list {folder_id}: {status or res.status_code}")
            except (requests.RequestException, ValueError) as exc:
                if attempt == 4:
                    raise DownloadError(f"Unable to list {folder_id}: {exc}") from exc
            if attempt < 4:
                time.sleep(retry_delay(attempt, res.headers.get("Retry-After") if 'res' in locals() else None))
        raise DownloadError(f"Gofile rate-limited listing for {folder_id}")

    def list_children(self, folder_id: str) -> tuple[dict, dict]:
        first = self._get_page(folder_id, 1)
        obj = first["data"]
        if obj.get("type") != "folder":
            return obj, {}
        result = {}
        page = 1
        while True:
            payload = first if page == 1 else self._get_page(folder_id, page)
            body = payload["data"]
            children = body.get("children", body.get("contents", {}))
            if not isinstance(children, dict):
                raise DownloadError(f"Unexpected children shape in {folder_id}")
            duplicates = result.keys() & children.keys()
            if duplicates:
                raise DownloadError(f"Duplicate pagination results in {folder_id}")
            result.update(children)
            meta = payload.get("metadata") or {}
            total_pages = meta.get("totalPages")
            if total_pages is not None:
                if page >= int(total_pages):
                    break
            elif len(children) < PAGE_SIZE:
                break
            # Defensive cap against broken pagination responses.
            page += 1
            if page > 10000:
                raise DownloadError(f"Too many pages in {folder_id}")
        return obj, result


@dataclass(frozen=True)
class DownloadItem:
    url: str
    path: Path
    size: int | None
    md5: str | None


def build_manifest(client: Gofile, code: str, root: Path, media: str = "all",
                   *, create_directories: bool = True,
                   cancel_event: threading.Event | None = None) -> list[DownloadItem]:
    """Discover full tree before starting downloads; stop on incomplete listings."""
    seen: set[str] = set()
    items: list[DownloadItem] = []

    def add_file(obj: dict, item_id: str, target: Path) -> None:
        check_cancel(cancel_event)
        if not matches_media(obj, target.name, media):
            return
        assert_safe_target(root, target)
        if obj.get("canAccess") is False:
            raise DownloadError(f"File {item_id} is inaccessible")
        link = obj.get("link") or obj.get("directLink")
        if not link:
            raise DownloadError(f"No download link for {item_id}")
        validate_gofile_url(link)
        size = int(obj["size"]) if obj.get("size") is not None else None
        if size is not None and size < 0:
            raise DownloadError(f"Invalid size for {item_id}")
        checksum = obj.get("md5")
        checksum = (checksum.lower() if isinstance(checksum, str)
                    and re.fullmatch(r"[0-9a-fA-F]{32}", checksum) else None)
        items.append(DownloadItem(link, target, size, checksum))

    def visit(cid: str, parent: Path, assigned_name: str | None = None) -> None:
        check_cancel(cancel_event)
        if cid in seen:
            raise DownloadError(f"Repeated/circular folder ID: {cid}")
        seen.add(cid)
        info, children = client.list_children(cid)
        if info.get("canAccess") is False:
            raise DownloadError(f"Folder {cid} is inaccessible")
        if info.get("type") == "file":
            if assigned_name is not None:
                raise DownloadError(f"Expected folder for {cid}")
            add_file(info, cid, parent / safe_name(info.get("name") or cid, cid))
            return
        if info.get("type") != "folder":
            raise DownloadError(f"Unknown content type for {cid}")
        directory = parent / (assigned_name or safe_name(info.get("name") or cid, cid))
        assert_safe_target(root, directory)
        if create_directories:
            directory.mkdir(parents=True, exist_ok=True)
        for child_id, obj, filename in unique_names(children):
            if obj.get("type") == "folder":
                visit(child_id, directory, filename)
            elif obj.get("type") == "file":
                add_file(obj, child_id, directory / filename)
            else:
                raise DownloadError(f"Unknown child type for {child_id}")

    base = root / code
    assert_safe_target(root, base)
    if create_directories:
        base.mkdir(parents=True, exist_ok=True)
    visit(code, base)
    return items


def digest_md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def completed(path: Path, item: DownloadItem) -> bool:
    if not path.exists():
        return False
    if not path.is_file():
        raise DownloadError(f"Non-file exists at output target: {path}")
    if item.size is None and not item.md5:
        raise DownloadError(f"File exists but cannot verify completeness: {path}")
    if item.size is not None and path.stat().st_size != item.size:
        raise DownloadError(f"File exists with different size (refusing overwrite): {path}")
    if item.md5 and digest_md5(path) != item.md5:
        raise DownloadError(f"Existing file MD5 does not match (refusing overwrite): {path}")
    return True


def download(item: DownloadItem, token: str, root: Path, dry_run: bool = False,
             *, cancel_event: threading.Event | None = None) -> str:
    check_cancel(cancel_event)
    assert_safe_target(root, item.path)
    if dry_run:
        return f"LIST {item.path} ({item.size if item.size is not None else '?'} bytes)"
    if completed(item.path, item):
        return f"SKIP {item.path} (size/checksum checked)"
    part = item.path.with_name(item.path.name + ".part")
    assert_safe_target(root, part)
    item.path.parent.mkdir(parents=True, exist_ok=True)
    with requests.Session() as session:
        for attempt in range(5):
            check_cancel(cancel_event)
            try:
                existing = part.stat().st_size if part.exists() else 0
                if item.size is not None and existing > item.size:
                    raise DownloadError(f"Part file exceeds expected size: {part}")
                headers = {"Cookie": f"accountToken={token}", "User-Agent": USER_AGENT,
                           "Referer": "https://gofile.io/"}
                if existing:
                    headers["Range"] = f"bytes={existing}-"
                url = item.url
                for redirects in range(6):
                    check_cancel(cancel_event)
                    validate_gofile_url(url)
                    response = session.get(url, headers=headers, timeout=(20, 90), stream=True,
                                           allow_redirects=False)
                    if response.status_code in (301, 302, 303, 307, 308):
                        new_url = urljoin(url, response.headers.get("Location", ""))
                        response.close()
                        validate_gofile_url(new_url)
                        url = new_url
                        continue
                    break
                else:
                    raise DownloadError(f"Too many redirects for {item.path}")
                with response as res:
                    if res.status_code in RETRYABLE:
                        if attempt == 4:
                            raise DownloadError(f"Download failed HTTP {res.status_code}: {item.path}")
                        pause(retry_delay(attempt, res.headers.get("Retry-After")), cancel_event)
                        continue
                    if res.status_code == 416 and item.size is not None and existing == item.size:
                        # The partial file appears complete; verify below.
                        pass
                    elif res.status_code not in (200, 206):
                        raise DownloadError(f"Download HTTP {res.status_code}: {item.path}")
                    else:
                        if res.status_code == 206:
                            match = re.match(r"bytes (\d+)-(\d+)/(\d+|\*)$",
                                             res.headers.get("Content-Range", ""))
                            if not match or int(match.group(1)) != existing:
                                raise DownloadError(f"Invalid Content-Range for {item.path}")
                        # A server may ignore Range and return 200: restart, don't append duplicates.
                        mode = "ab" if res.status_code == 206 else "wb"
                        with part.open(mode) as fp:
                            for chunk in res.iter_content(chunk_size=1024 * 1024):
                                check_cancel(cancel_event)
                                if chunk:
                                    fp.write(chunk)
                if item.size is not None and part.stat().st_size != item.size:
                    actual = part.stat().st_size
                    if actual < item.size and attempt < 4:
                        # Keep the partial bytes and resume on the next attempt.
                        pause(retry_delay(attempt), cancel_event)
                        continue
                    raise DownloadError(f"Size mismatch for {item.path}: got {actual}, expected {item.size}")
                if item.md5 and digest_md5(part) != item.md5:
                    raise DownloadError(f"MD5 mismatch for {item.path}; partial file kept")
                check_cancel(cancel_event)
                if item.path.exists():
                    raise DownloadError(f"Output appeared while downloading; refusing overwrite: {item.path}")
                part.rename(item.path)
                return f"OK   {item.path} ({item.path.stat().st_size} bytes)"
            except (requests.RequestException, OSError) as exc:
                if attempt == 4:
                    raise DownloadError(f"Failed after 5 attempts, {item.path}: {exc}") from exc
                pause(retry_delay(attempt), cancel_event)
        raise DownloadError(f"Download failed: {item.path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gofile recursive bulk downloader")
    parser.add_argument("urls", nargs="*", metavar="ID_OR_URL",
                        help="Gofile folder ID or share URL (prompts when omitted)")
    parser.add_argument("-o", "--output", default="downloads", help="Destination directory")
    parser.add_argument("-w", "--workers", type=int, default=3, help="Parallel file downloads (1-8)")
    parser.add_argument("--media", choices=("videos", "photos", "all"),
                        help="Choose file category without the interactive menu")
    parser.add_argument("--dry-run", action="store_true", help="List files without downloading")
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 8:
        parser.error("--workers must be between 1 and 8")
    try:
        supplied = args.urls if args.urls else prompt_shares()
        codes = parse_shares(" ".join(supplied))
        # Show the selection menu when launched interactively. For scripted
        # invocations with stdin redirected, default to all files.
        media = args.media or (prompt_media() if sys.stdin.isatty() or not args.urls else "all")
        root = Path(args.output).expanduser().resolve()
        if not args.dry_run:
            root.mkdir(parents=True, exist_ok=True)
        client = Gofile(os.getenv("GOFILE_TOKEN"), os.getenv("GOFILE_PASSWORD"))
        manifest = []
        for url, code in codes:
            print(f"Listing {url} ({media})", flush=True)
            manifest.extend(build_manifest(client, code, root, media,
                                           create_directories=not args.dry_run))
        print(f"Found {len(manifest)} matching files; total listed size: "
              f"{sum(x.size or 0 for x in manifest):,} bytes", flush=True)
        failures = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(download, item, client.token, root, args.dry_run)
                       for item in manifest]
            for f in as_completed(futures):
                try:
                    print(f.result(), flush=True)
                except Exception as exc:
                    failures += 1
                    print(f"ERROR {exc}", file=sys.stderr, flush=True)
        print(f"Finished: {len(manifest) - failures} succeeded/skipped, {failures} failed")
        return 1 if failures else 0
    except (DownloadError, requests.RequestException, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def run_gui() -> int:
    """Open an optional Tkinter desktop UI. No extra packages beyond requests."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk
    import subprocess

    try:
        window = tk.Tk()
    except tk.TclError as exc:
        raise RuntimeError(f"Cannot open desktop window: {exc}") from exc

    window.title("Gofile Downloader")
    window.geometry("790x730")
    window.minsize(640, 615)
    window.configure(bg="#f5f7fb")

    style = ttk.Style(window)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    style.configure("TFrame", background="#f5f7fb")
    style.configure("TLabel", background="#f5f7fb", foreground="#182538",
                    font=("Segoe UI", 10))
    style.configure("Title.TLabel", font=("Segoe UI", 19, "bold"),
                    foreground="#152238")
    style.configure("Section.TLabel", font=("Segoe UI", 11, "bold"))
    style.configure("Hint.TLabel", font=("Segoe UI", 9), foreground="#64748b")
    style.configure("Status.TLabel", font=("Segoe UI", 10, "bold"))
    style.configure("TButton", font=("Segoe UI", 10), padding=(12, 7))
    style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"), padding=(16, 8),
                    background="#2563eb", foreground="white")
    style.map("Primary.TButton", background=[("active", "#1d4ed8")],
              foreground=[("active", "white")])
    style.configure("TProgressbar", thickness=16)

    outer = ttk.Frame(window, padding=24)
    outer.pack(fill="both", expand=True)
    ttk.Label(outer, text="Gofile Downloader", style="Title.TLabel").pack(anchor="w")
    ttk.Label(outer, text="Paste a folder link, choose your files, and click Download.",
              style="Hint.TLabel").pack(anchor="w", pady=(3, 18))

    ttk.Label(outer, text="1   Folder link or ID", style="Section.TLabel").pack(anchor="w")
    ttk.Label(outer, text="Example: kaHyq5zp or https://gofile.io/d/kaHyq5zp. Multiple links are supported.",
              style="Hint.TLabel").pack(anchor="w", pady=(3, 5))
    links = tk.Text(outer, height=3, wrap="word", font=("Segoe UI", 11),
                    relief="solid", bd=1, padx=10, pady=8, undo=True)
    links.pack(fill="x")
    links.focus_set()

    ttk.Label(outer, text="2   What should be downloaded?", style="Section.TLabel").pack(
        anchor="w", pady=(17, 5))
    selected_media = tk.StringVar(value="all")
    filters = ttk.Frame(outer)
    filters.pack(fill="x")
    for label, value in (("All files", "all"), ("Videos only", "videos"),
                         ("Photos only", "photos")):
        ttk.Radiobutton(filters, text=label, value=value, variable=selected_media).pack(
            side="left", padx=(0, 26))

    ttk.Label(outer, text="3   Save files to", style="Section.TLabel").pack(
        anchor="w", pady=(17, 5))
    default_folder = Path.home() / "Downloads"
    if not default_folder.is_dir():
        default_folder = Path.cwd()
    target_dir = tk.StringVar(value=str(default_folder / "Gofile"))
    destination = ttk.Frame(outer)
    destination.pack(fill="x")
    path_entry = ttk.Entry(destination, textvariable=target_dir, font=("Segoe UI", 10))
    path_entry.pack(side="left", fill="x", expand=True)

    def browse() -> None:
        folder = filedialog.askdirectory(title="Choose a download folder",
                                         initialdir=str(default_folder))
        if folder:
            target_dir.set(folder)

    browse_btn = ttk.Button(destination, text="Browse...", command=browse)
    browse_btn.pack(side="left", padx=(8, 0))

    advanced_enabled = tk.BooleanVar(value=False)
    advanced = ttk.Frame(outer)
    advanced_switch = ttk.Checkbutton(outer, text="Advanced options (parallel downloads / credentials)",
                                      variable=advanced_enabled)
    advanced_switch.pack(anchor="w", pady=(12, 0))

    workers_var = tk.StringVar(value="3")
    password_var = tk.StringVar(value="")
    token_var = tk.StringVar(value="")
    ttk.Label(advanced, text="Parallel files (1-8):").grid(row=0, column=0, sticky="w", pady=4)
    workers_entry = ttk.Spinbox(advanced, from_=1, to=8, width=5,
                                textvariable=workers_var)
    workers_entry.grid(row=0, column=1, sticky="w", padx=8)
    ttk.Label(advanced, text="Folder password (if needed):").grid(row=1, column=0, sticky="w", pady=4)
    password_entry = ttk.Entry(advanced, textvariable=password_var, show="*", width=32)
    password_entry.grid(row=1, column=1, sticky="ew", padx=8)
    ttk.Label(advanced, text="Account token (optional):").grid(row=2, column=0, sticky="w", pady=4)
    token_entry = ttk.Entry(advanced, textvariable=token_var, show="*", width=32)
    token_entry.grid(row=2, column=1, sticky="ew", padx=8)
    advanced.grid_columnconfigure(1, weight=1)

    def toggle_advanced(*_args: object) -> None:
        if advanced_enabled.get():
            advanced.pack(fill="x", pady=(5, 0))
        else:
            advanced.pack_forget()

    advanced_enabled.trace_add("write", toggle_advanced)

    buttons = ttk.Frame(outer)
    buttons.pack(fill="x", pady=(16, 12))
    start_btn = ttk.Button(buttons, text="Download", style="Primary.TButton")
    start_btn.pack(side="left")
    preview_btn = ttk.Button(buttons, text="Preview files")
    preview_btn.pack(side="left", padx=(10, 0))
    cancel_btn = ttk.Button(buttons, text="Cancel", state="disabled")
    cancel_btn.pack(side="left", padx=(10, 0))
    open_btn = ttk.Button(buttons, text="Open folder")
    open_btn.pack(side="right")

    progress_label = tk.StringVar(value="Ready to download")
    ttk.Label(outer, textvariable=progress_label, style="Status.TLabel").pack(anchor="w")
    progress = ttk.Progressbar(outer, orient="horizontal", mode="determinate", maximum=1)
    progress.pack(fill="x", pady=(7, 5))
    summary_var = tk.StringVar(value="No files scanned yet")
    ttk.Label(outer, textvariable=summary_var, style="Hint.TLabel").pack(anchor="w")
    ttk.Label(outer, text="Activity", style="Section.TLabel").pack(anchor="w", pady=(14, 5))
    log_box = scrolledtext.ScrolledText(outer, wrap="word", font=("Consolas", 9),
                                        height=9, state="disabled", bd=1, relief="solid")
    log_box.pack(fill="both", expand=True)

    messages: queue.Queue[tuple[str, object]] = queue.Queue()
    stop = threading.Event()
    active = False
    close_after_stop = False
    total = finished = successes = skipped = failed = 0

    def log(message: str) -> None:
        log_box.configure(state="normal")
        log_box.insert("end", message.rstrip() + "\n")
        if int(log_box.index("end-1c").split(".")[0]) > 400:
            log_box.delete("1.0", "101.0")
        log_box.see("end")
        log_box.configure(state="disabled")

    def set_busy(state: bool) -> None:
        nonlocal active
        active = state
        normal = "disabled" if state else "normal"
        for widget in (start_btn, preview_btn, browse_btn, path_entry, links,
                       advanced_switch, workers_entry, password_entry, token_entry):
            widget.configure(state=normal)
        cancel_btn.configure(state="normal" if state else "disabled")

    def open_folder() -> None:
        try:
            folder = Path(target_dir.get()).expanduser().resolve()
            if not folder.is_dir():
                messagebox.showinfo("Folder not created", "This download folder does not exist yet.")
                return
            if sys.platform == "win32":
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except (OSError, ValueError) as exc:
            messagebox.showerror("Open folder failed", str(exc))

    def worker(mode: str, codes: list[tuple[str, str]], root: Path, media: str,
               workers: int, token: str | None, password: str | None) -> None:
        try:
            messages.put(("log", "Connecting to Gofile and scanning folder contents..."))
            check_cancel(stop)
            client = Gofile(token, password)
            found: list[DownloadItem] = []
            if mode == "download":
                root.mkdir(parents=True, exist_ok=True)
            for url, code in codes:
                check_cancel(stop)
                messages.put(("log", f"Scanning {url}"))
                found.extend(build_manifest(client, code, root, media,
                                            create_directories=(mode == "download"),
                                            cancel_event=stop))
            known = sum(item.size for item in found if item.size is not None)
            unknown = sum(item.size is None for item in found)
            messages.put(("scan", (len(found), known, unknown)))
            if mode == "preview":
                for item in found[:25]:
                    messages.put(("log", f"  {item.path.relative_to(root)}"))
                if len(found) > 25:
                    messages.put(("log", f"  ... and {len(found) - 25} more"))
                messages.put(("end", ("preview", False)))
                return
            if not found:
                messages.put(("end", ("download", False)))
                return
            with ThreadPoolExecutor(max_workers=workers) as pool:
                tasks = [pool.submit(download, item, client.token, root,
                                     cancel_event=stop) for item in found]
                for task in as_completed(tasks):
                    try:
                        outcome = task.result()
                        kind = "skip" if outcome.startswith("SKIP") else "ok"
                        messages.put(("file", (kind, outcome)))
                    except DownloadCancelled:
                        messages.put(("file", ("cancel", "Cancelled")))
                    except Exception as exc:
                        messages.put(("file", ("error", f"ERROR: {exc}")))
            messages.put(("end", ("download", stop.is_set())))
        except DownloadCancelled:
            messages.put(("end", (mode, True)))
        except Exception as exc:
            messages.put(("fatal", str(exc)))

    def start(mode: str) -> None:
        nonlocal total, finished, successes, skipped, failed
        if active:
            return
        try:
            codes = parse_shares(links.get("1.0", "end"))
            folder_raw = target_dir.get().strip()
            if not folder_raw:
                raise DownloadError("Choose a destination folder.")
            root = Path(folder_raw).expanduser().resolve()
            if root.exists() and not root.is_dir():
                raise DownloadError("The destination must be a directory, not a file.")
            workers = int(workers_var.get())
            if not 1 <= workers <= 8:
                raise DownloadError("Parallel downloads must be between 1 and 8.")
        except (DownloadError, ValueError, OSError) as exc:
            messagebox.showwarning("Check your selection", str(exc))
            return
        total = finished = successes = skipped = failed = 0
        stop.clear()
        progress.configure(maximum=1, value=0)
        progress_label.set("Scanning folders..." if mode == "preview" else "Preparing downloads...")
        summary_var.set(f"{len(codes)} folder(s) selected")
        log("=" * 45)
        log("Previewing files" if mode == "preview" else "Starting download")
        set_busy(True)
        threading.Thread(target=worker, daemon=True,
                         args=(mode, codes, root, selected_media.get(), workers,
                               token_var.get().strip() or os.getenv("GOFILE_TOKEN"),
                               password_var.get() or os.getenv("GOFILE_PASSWORD"))).start()

    def request_cancel() -> None:
        if active:
            stop.set()
            progress_label.set("Cancelling... active requests may take a moment to stop")
            cancel_btn.configure(state="disabled")
            log("Cancellation requested. Partial files will be kept for resuming.")

    def on_close() -> None:
        nonlocal close_after_stop
        if active:
            if messagebox.askyesno("Cancel download?", "Cancel current transfers and close the app?"):
                close_after_stop = True
                request_cancel()
        else:
            window.destroy()

    def pump() -> None:
        nonlocal total, finished, successes, skipped, failed
        try:
            for _ in range(150):
                try:
                    kind, value = messages.get_nowait()
                except queue.Empty:
                    break
                if kind == "log":
                    log(str(value))
                elif kind == "scan":
                    total, known, unknown = value
                    progress.configure(maximum=max(1, total), value=0)
                    extra = f" (+ {unknown} files with unknown size)" if unknown else ""
                    summary_var.set(f"{total} matching file(s) | {format_size(known)} listed{extra}")
                    if not total:
                        log("No files matched the selected filter.")
                    elif not stop.is_set():
                        progress_label.set(f"Found {total} files" if not active else
                                           f"Discovered {total} files")
                elif kind == "file":
                    classification, line = value
                    finished += 1
                    if classification == "ok":
                        successes += 1
                    elif classification == "skip":
                        skipped += 1
                    elif classification == "error":
                        failed += 1
                    if classification != "cancel":
                        log(str(line))
                    progress.configure(value=finished)
                    progress_label.set(f"Processed {finished} / {total} files")
                    summary_var.set(f"Downloaded: {successes}   Already there: {skipped}   Failed: {failed}")
                elif kind == "fatal":
                    log(f"ERROR: {value}")
                    progress_label.set("Could not complete the request")
                    set_busy(False)
                elif kind == "end":
                    mode, was_cancelled = value
                    if was_cancelled:
                        progress_label.set("Cancelled. You can resume by downloading again.")
                    elif mode == "preview":
                        progress_label.set(f"Preview complete: {total} matching files")
                        log("Preview only. No files were downloaded or folders created.")
                    else:
                        progress_label.set(f"Finished: {successes} downloaded, {skipped} skipped, {failed} failed")
                        log(f"Finished: {successes} downloaded, {skipped} already present, {failed} failed.")
                    set_busy(False)
                if close_after_stop and not active:
                    window.destroy()
                    return
        except tk.TclError:
            return
        window.after(100, pump)

    start_btn.configure(command=lambda: start("download"))
    preview_btn.configure(command=lambda: start("preview"))
    cancel_btn.configure(command=request_cancel)
    open_btn.configure(command=open_folder)
    window.protocol("WM_DELETE_WINDOW", on_close)
    window.after(100, pump)
    window.mainloop()
    return 0


def entrypoint() -> int:
    """No arguments: desktop app. Any CLI arguments: existing CLI behavior."""
    if len(sys.argv) > 1:
        return main()
    try:
        return run_gui()
    except (ImportError, RuntimeError) as exc:
        if sys.stdin.isatty():
            print(f"GUI unavailable ({exc}). Switching to interactive terminal mode.")
            return main([])
        print(f"Cannot open Gofile interface: {exc}. Try passing an ID on the command line.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(entrypoint())
