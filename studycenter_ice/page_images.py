"""Rebuildable PDF previews; original bytes and render settings define each image."""
import hashlib
import logging
from pathlib import Path
import tempfile
import threading

import pymupdf


class PageImages:
    def __init__(self, data_dir, *, max_bytes=128 * 1024 * 1024, max_entries=128):
        self.directory = Path(data_dir) / 'cache' / 'pdf-pages'
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.gate = threading.Lock()

    def image(self, original_path, page):
        if page < 1:
            return None, None
        # Also detect a restored/replaced file: never trust a stale DB digest alone.
        raw = Path(original_path).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        key = hashlib.sha256(f'{digest}:{page}:125:png:{pymupdf.VersionBind}'.encode()).hexdigest()
        etag = f'"{key}"'
        target = self.directory / f'{key}.png'
        with self.gate:
            try:
                png = target.read_bytes()
                if png.startswith(b'\x89PNG\r\n\x1a\n'):
                    target.touch()  # Retain frequently read previews when pruning.
                    return png, etag
            except OSError:
                pass
            with pymupdf.open(stream=raw, filetype='pdf') as pdf:
                if page > len(pdf):
                    return None, None
                png = pdf[page-1].get_pixmap(dpi=125).tobytes('png')
            temporary = None
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                if len(png) <= self.max_bytes:
                    with tempfile.NamedTemporaryFile(dir=self.directory, suffix='.tmp', delete=False) as output:
                        temporary = Path(output.name)
                        output.write(png)
                    temporary.replace(target)
                    self._prune()
            except OSError as error:
                logging.getLogger(__name__).warning('PDF-Bildcache nicht verfügbar (%s).', type(error).__name__)
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
            return png, etag

    def _prune(self):
        entries = [(path, path.stat()) for path in self.directory.glob('*.png')]
        entries.sort(key=lambda entry: entry[1].st_mtime_ns, reverse=True)
        used = 0
        for index, (path, stat) in enumerate(entries):
            used += stat.st_size
            if index >= self.max_entries or used > self.max_bytes:
                path.unlink(missing_ok=True)
