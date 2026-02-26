#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Mirror (download + sync) a remote HTTP directory listing (and subfolders) into a local folder.

Features
- Full recursive download of all files/subfolders.
- Sync mode: compare local files with server files and download:
  - missing files
  - files that changed (based on ETag / Last-Modified / Content-Length when available)

Requirements:
- Python 3.9+
- requests, beautifulsoup4
  pip install requests beautifulsoup4
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup


# =========================
# CONFIG (edit here)
# =========================
TARGET_URL = "https://websetup.froggitti.net/ota-storage/"  # <-- remote target to mirror
DEST_DIR = "/Volumes/NonSC_MFMG3/OTA"                        # <--  local folder to store the downloaded files

# Optional behavior
USER_AGENT = "webFolderMirror/1.0 (+https://example.local)"
TIMEOUT_SECS = 60
VERIFY_TLS = True
DRY_RUN = False  # if True: do not write files, just show what would happen


# =========================
# Internal structures
# =========================
@dataclass(frozen=True)
class RemoteFile:
    url: str
    rel_path: str  # relative to DEST_DIR
    size: Optional[int] = None
    etag: Optional[str] = None
    last_modified: Optional[datetime] = None


@dataclass
class TransferProgress:
    overall_done: int = 0
    overall_total: int = 0


# =========================
# Helpers
# =========================
def safe_rel_path_from_url(base_url: str, file_url: str) -> str:
    """
    Convert an absolute file URL under base_url into a safe relative filesystem path.
    """
    b = urlparse(base_url)
    u = urlparse(file_url)
    if (u.scheme, u.netloc) != (b.scheme, b.netloc):
        raise ValueError(f"URL outside base host: {file_url}")

    base_path = b.path
    path = u.path
    if not path.startswith(base_path):
        raise ValueError(f"URL outside base path: {file_url}")

    rel = path[len(base_path):]
    rel = unquote(rel)
    rel = rel.lstrip("/")
    rel = rel.replace("\\", "/")
    rel = re.sub(r"/+", "/", rel)
    if rel.startswith("../") or "/../" in rel or rel == "..":
        raise ValueError(f"Unsafe relative path derived from URL: {file_url}")
    return rel


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def parse_http_datetime(value: str) -> Optional[datetime]:
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def format_bytes(num_bytes: Optional[int]) -> str:
    if num_bytes is None:
        return "inconnue"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num_bytes)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{num_bytes} B"


def render_progress_bar(done: int, total: Optional[int], width: int = 18) -> str:
    if total is None or total <= 0:
        return f"[{'?' * width}] n/a"
    ratio = min(max(done / total, 0.0), 1.0)
    filled = int(ratio * width)
    bar = "#" * filled + "." * (width - filled)
    return f"[{bar}] {ratio * 100:6.2f}%"


def print_progress_line(rel_path: str, file_done: int, file_total: Optional[int], transfer: TransferProgress) -> None:
    file_bar = render_progress_bar(file_done, file_total)
    global_bar = render_progress_bar(transfer.overall_done, transfer.overall_total if transfer.overall_total > 0 else None)
    msg = (
        f"\r{rel_path} | fichier {file_bar} ({format_bytes(file_done)}/{format_bytes(file_total)})"
        f" | total {global_bar} ({format_bytes(transfer.overall_done)}/{format_bytes(transfer.overall_total if transfer.overall_total > 0 else None)})"
    )
    print(msg, end="", flush=True)


# =========================
# Directory listing crawler
# =========================
def list_directory_links(html: str, page_url: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    hrefs: List[str] = []
    for a in soup.select("a[href]"):
        href_raw = a.get("href")
        if not isinstance(href_raw, str) or not href_raw:
            continue
        href = href_raw
        if href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
            continue
        if href.startswith("?"):
            continue
        abs_url = urljoin(page_url, href)
        hrefs.append(abs_url)
    return hrefs


def is_probably_directory_url(url: str) -> bool:
    return urlparse(url).path.endswith("/")


def crawl_remote_files(base_url: str) -> List[RemoteFile]:
    s = session()
    seen_dirs = set()
    to_visit = [base_url]
    files: List[RemoteFile] = []

    while to_visit:
        dir_url = to_visit.pop()
        if dir_url in seen_dirs:
            continue
        seen_dirs.add(dir_url)

        r = s.get(dir_url, timeout=TIMEOUT_SECS, verify=VERIFY_TLS)
        r.raise_for_status()

        links = list_directory_links(r.text, dir_url)

        for link in links:
            lp = urlparse(link).path
            if lp.endswith("/../") or lp.endswith("..") or lp.endswith("../"):
                continue

            try:
                rel = safe_rel_path_from_url(base_url, link)
            except ValueError:
                continue

            if rel == "":
                continue

            if is_probably_directory_url(link):
                to_visit.append(link)
            else:
                files.append(RemoteFile(url=link, rel_path=rel))

    return files


# =========================
# Remote metadata + sync
# =========================
def head_remote(s: requests.Session, url: str) -> Tuple[Optional[int], Optional[str], Optional[datetime]]:
    try:
        r = s.head(url, allow_redirects=True, timeout=TIMEOUT_SECS, verify=VERIFY_TLS)
        if r.status_code >= 400:
            return None, None, None
        size = r.headers.get("Content-Length")
        etag = r.headers.get("ETag")
        lm = r.headers.get("Last-Modified")
        return (
            int(size) if size and size.isdigit() else None,
            etag,
            parse_http_datetime(lm) if lm else None,
        )
    except Exception:
        return None, None, None


def local_file_signature(path: Path) -> Tuple[Optional[int], Optional[datetime]]:
    if not path.exists() or not path.is_file():
        return None, None
    st = path.stat()
    return st.st_size, datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)


def should_download(
    local_path: Path,
    remote_size: Optional[int],
    remote_etag: Optional[str],
    remote_last_modified: Optional[datetime],
    state_db: Dict[str, Dict[str, str]],
) -> bool:
    if not local_path.exists():
        return True

    rel = local_path.as_posix()
    local_size, local_mtime = local_file_signature(local_path)

    if remote_etag:
        prev = state_db.get(rel, {}).get("etag")
        if prev and prev != remote_etag:
            return True

    if remote_last_modified and local_mtime:
        if remote_last_modified.timestamp() > (local_mtime.timestamp() + 2):
            return True

    if remote_size is not None and local_size is not None and remote_size != local_size:
        return True

    return False


def load_state(dest_dir: Path) -> Dict[str, Dict[str, str]]:
    state_path = dest_dir / ".ota_mirror_state.tsv"
    db: Dict[str, Dict[str, str]] = {}
    if not state_path.exists():
        return db

    for line in state_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        rel = parts[0]
        db[rel] = {
            "etag": parts[1] if len(parts) > 1 else "",
            "last_modified": parts[2] if len(parts) > 2 else "",
            "size": parts[3] if len(parts) > 3 else "",
        }
    return db


def save_state(dest_dir: Path, db: Dict[str, Dict[str, str]]) -> None:
    state_path = dest_dir / ".ota_mirror_state.tsv"
    lines: List[str] = []
    for rel, meta in sorted(db.items(), key=lambda x: x[0]):
        lines.append("\t".join([rel, meta.get("etag", "") or "", meta.get("last_modified", "") or "", meta.get("size", "") or ""]))
    if DRY_RUN:
        return
    state_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def download_file(
    s: requests.Session,
    remote: RemoteFile,
    dest_dir: Path,
    state_db: Dict[str, Dict[str, str]],
    transfer: TransferProgress,
) -> None:
    local_path = dest_dir / remote.rel_path
    ensure_parent_dir(local_path)

    if DRY_RUN:
        print(f"[DRY-RUN] download: {remote.url} -> {local_path} ({format_bytes(remote.size)})")
        return

    with s.get(remote.url, stream=True, timeout=TIMEOUT_SECS, verify=VERIFY_TLS) as r:
        r.raise_for_status()
        file_total = remote.size
        if file_total is None:
            header_size = r.headers.get("Content-Length")
            if header_size and header_size.isdigit():
                file_total = int(header_size)
                transfer.overall_total += file_total

        tmp_path = local_path.with_suffix(local_path.suffix + ".part")
        file_done = 0
        with open(tmp_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
                    chunk_len = len(chunk)
                    file_done += chunk_len
                    transfer.overall_done += chunk_len
                    print_progress_line(remote.rel_path, file_done, file_total, transfer)
        os.replace(tmp_path, local_path)
        print()

        lm = r.headers.get("Last-Modified")
        dt = parse_http_datetime(lm) if lm else None
        if dt:
            os.utime(local_path, (time.time(), dt.timestamp()))

        rel = local_path.as_posix()
        state_db[rel] = {
            "etag": r.headers.get("ETag", "") or "",
            "last_modified": (dt.isoformat() if dt else "") or "",
            "size": r.headers.get("Content-Length", "") or str(local_path.stat().st_size),
        }

    print(f"downloaded: {remote.rel_path} ({format_bytes(file_done)})")


def sync(target_url: str, dest_dir: Path) -> None:
    # Normalize to ensure trailing slash (important for rel paths and recursion)
    if not target_url.endswith("/"):
        target_url += "/"

    dest_dir.mkdir(parents=True, exist_ok=True)
    s = session()
    state_db = load_state(dest_dir)

    print(f"crawl: {target_url}")
    remote_files = crawl_remote_files(target_url)
    print(f"found {len(remote_files)} files")

    enriched: List[RemoteFile] = []
    for rf in remote_files:
        size, etag, lm = head_remote(s, rf.url)
        enriched.append(RemoteFile(url=rf.url, rel_path=rf.rel_path, size=size, etag=etag, last_modified=lm))

    total_remote_known = sum(rf.size for rf in enriched if rf.size is not None)
    total_remote_unknown = sum(1 for rf in enriched if rf.size is None)
    if total_remote_unknown:
        print(
            f"taille totale du répertoire distant (connue): {format_bytes(total_remote_known)} "
            f"(+ {total_remote_unknown} fichier(s) de taille inconnue)"
        )
    else:
        print(f"taille totale du répertoire distant: {format_bytes(total_remote_known)}")

    to_download: List[RemoteFile] = []
    total_download_known = 0
    total_download_unknown = 0
    for rf in enriched:
        local_path = dest_dir / rf.rel_path
        if should_download(local_path, rf.size, rf.etag, rf.last_modified, state_db):
            to_download.append(rf)
            if rf.size is None:
                total_download_unknown += 1
            else:
                total_download_known += rf.size

    if total_download_unknown:
        print(
            f"à télécharger maintenant (taille connue): {format_bytes(total_download_known)} "
            f"(+ {total_download_unknown} fichier(s) de taille inconnue)"
        )
    else:
        print(f"à télécharger maintenant: {format_bytes(total_download_known)}")

    downloads = 0
    transfer = TransferProgress(overall_done=0, overall_total=total_download_known)
    for rf in to_download:
        print(f"need: {rf.rel_path} ({format_bytes(rf.size)})")
        download_file(s, rf, dest_dir, state_db, transfer)
        downloads += 1

    save_state(dest_dir, state_db)
    print(f"done. downloaded/updated: {downloads}")


def main() -> int:
    dest = Path(DEST_DIR).expanduser().resolve()
    try:
        sync(TARGET_URL, dest)
        return 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())