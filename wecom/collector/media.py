"""Resolve verified WeCom 5.0.13 media references inside the account cache only."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import ntpath
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from proto import fields, text, integer, strings


def _digest_text(value):
    if isinstance(value, bytes):
        if len(value) == 16:
            return value.hex()
        value = value.decode('ascii', errors='ignore')
    value = str(value or '').lower()
    return value if re.fullmatch('[0-9a-f]{32}', value) else ''


def image_format(header):
    if header.startswith(b'\xff\xd8\xff'):
        return 'jpeg'
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if header.startswith((b'GIF87a', b'GIF89a')):
        return 'gif'
    if header.startswith(b'RIFF') and header[8:12] == b'WEBP':
        return 'webp'
    if header.startswith((b'BM', b'II*\0', b'MM\0*')):
        return 'bitmap'
    return None


def _decoded_cache_variant(path, original_dimensions):
    from PIL import Image
    original_width, original_height = original_dimensions
    if original_width <= 0 or original_height <= 0:
        raise ValueError('Image has no declared original dimensions')
    with Image.open(path) as image:
        width, height = image.size
        image.load()
        format_name = image.format.lower()

    def proportional(ow, oh):
        return abs(width * oh - height * ow) <= ow + oh

    if not (proportional(original_width, original_height) or proportional(original_height, original_width)):
        raise ValueError('Cached image dimensions do not match its source aspect')
    return {'original': False, 'variant': 'client_cache',
            'dimensions': [width, height], 'original_dimensions': list(original_dimensions),
            'image_format': format_name, 'verification': 'cache_reference_and_decoded_dimensions'}


class MediaIndex:
    def __init__(self, snapshot_dir, account_root, output_dir=None):
        self.account_root = Path(account_root)
        self.cache_root = (self.account_root / 'Cache').resolve()
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self.by_digest = defaultdict(list)
        self.by_key = defaultdict(list)
        self.mappings = []
        self._basenames = None
        self._checked = {}
        for path in sorted(Path(snapshot_dir).glob('cache-*.db')):
            db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
            db.row_factory = sqlite3.Row
            try:
                for row in db.execute('SELECT type,key,file_name,file_md5 FROM mapping'):
                    value = dict(row)
                    self.mappings.append(value)
                    self.by_key[str(value['key'])].append(value)
                    digest = _digest_text(value['file_md5'])
                    if digest:
                        self.by_digest[digest].append(value)
            finally:
                db.close()

    def _allowed(self, path):
        path = Path(path)
        if str(path).startswith(('\\\\', '//')):
            return None
        try:
            resolved = path.resolve()
            if os.path.commonpath((os.path.normcase(str(resolved)), os.path.normcase(str(self.cache_root)))) != os.path.normcase(str(self.cache_root)):
                return None
        except (OSError, ValueError):
            return None
        return resolved

    def _find_paths(self, name):
        normalized = str(name).replace('\\', os.sep).replace('/', os.sep)
        candidates = [Path(normalized)] if os.path.isabs(normalized) else [self.cache_root / normalized, self.account_root / normalized]
        found = []
        for candidate in candidates:
            allowed = self._allowed(candidate)
            if allowed is not None and allowed.is_file():
                found.append(allowed)
        if found:
            return found
        if self._basenames is None:
            self._basenames = defaultdict(list)
            for directory, children, files in os.walk(self.cache_root, followlinks=False):
                children[:] = [child for child in children
                               if not Path(directory, child).is_symlink()
                               and not (hasattr(os.path, 'isjunction') and os.path.isjunction(Path(directory, child)))]
                for filename in files:
                    allowed = self._allowed(Path(directory, filename))
                    if allowed is not None:
                        self._basenames[filename.casefold()].append(allowed)
        basename = ntpath.basename(str(name).replace('/', '\\')).casefold()
        return list(self._basenames.get(basename, ()))

    def _check_and_copy(self, path, expected_md5, expected_size, extension, image_dimensions=None):
        stat = path.stat()
        token = (str(path), stat.st_size, stat.st_mtime_ns, expected_md5, expected_size, image_dimensions)
        if token in self._checked:
            return self._checked[token]
        temporary = None
        destination_stream = None
        if self.output_dir is not None:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix='.media-', dir=self.output_dir)
            destination_stream = os.fdopen(descriptor, 'wb')
        try:
            md5, sha256 = hashlib.md5(), hashlib.sha256()
            size = 0
            header = b''
            with path.open('rb') as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    if not header:
                        header = chunk[:32]
                    md5.update(chunk)
                    sha256.update(chunk)
                    size += len(chunk)
                    if destination_stream is not None:
                        destination_stream.write(chunk)
            if destination_stream is not None:
                destination_stream.flush()
            after = path.stat()
            if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                return {'status': 'pending_download', 'reason': 'cache_changed_during_read'}
            validation = {'original': True, 'variant': 'original', 'verification': 'source_md5_and_size'}
            size_matches = not expected_size or size == expected_size
            digest_matches = bool(expected_md5) and md5.hexdigest() == expected_md5
            if not (size_matches and digest_matches):
                if image_dimensions is None:
                    reason = 'size_mismatch' if not size_matches else 'checksum_mismatch'
                    return {'status': 'pending_download', 'reason': reason, 'actual_size': size}
                validation = _decoded_cache_variant(Path(temporary) if temporary is not None else path, image_dimensions)
                validation.update(original_size=expected_size, original_md5=expected_md5)
                current = path.stat()
                if (stat.st_size, stat.st_mtime_ns) != (current.st_size, current.st_mtime_ns):
                    return {'status': 'pending_download', 'reason': 'cache_changed_during_decode'}
            if destination_stream is not None:
                destination_stream.flush()
                os.fsync(destination_stream.fileno())
                destination_stream.close()
                destination_stream = None
                stored_extension = extension if validation['original'] else {'jpeg': '.jpg', 'png': '.png', 'gif': '.gif', 'webp': '.webp'}.get(validation['image_format'], extension)
                destination = self.output_dir / (sha256.hexdigest() + stored_extension)
                os.replace(temporary, destination)
                temporary = None
            else:
                destination = path
            if image_dimensions is not None:
                try:
                    from PIL import Image
                    with Image.open(destination) as image:
                        actual_dimensions = [int(image.size[0]), int(image.size[1])]
                    validation['dimensions'] = actual_dimensions
                    validation['original_dimensions'] = [int(image_dimensions[0]), int(image_dimensions[1])]
                except Exception:
                    pass
            result = {'status': 'ready', 'path': str(destination), 'size': size,
                      'md5': md5.hexdigest(), 'sha256': sha256.hexdigest(),
                      'image_format': image_format(header), **validation}
            self._checked[token] = result
            return result
        finally:
            if destination_stream is not None:
                destination_stream.close()
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def resolve_metadata(self, *, kind, name, size, digest, file_id='', local_extra=b''):
        return self._resolve(kind, name, size, digest, file_id, local_extra, None)

    def resolve(self, kind, payload, local_extra=b''):
        parsed = fields(payload)
        file_id = text(parsed, 1)
        name = text(parsed, 2) or ('image' if kind == 'image' else 'file')
        size = integer(parsed, 4)
        digest = _digest_text(text(parsed, 10))
        dimensions = None
        if kind == 'image':
            width, height = integer(parsed, 5), integer(parsed, 6)
            if width > 0 and height > 0:
                dimensions = (width, height)
        return self._resolve(kind, name, size, digest, file_id, local_extra, dimensions)

    def _resolve(self, kind, name, size, digest, file_id, local_extra, dimensions):
        attachment = {'kind': kind, 'name': name, 'size': size, 'md5': digest,
                      'status': 'pending_download', 'source_ref_hash': hashlib.sha256(str(file_id or '').encode()).hexdigest()}
        mappings = list(self.by_digest.get(digest, ()))
        mappings += self.by_key.get(file_id, ())
        if file_id:
            mappings += [row for row in self.mappings if isinstance(row['key'], str) and file_id in row['key']]
        names = [row['file_name'] for row in mappings]
        if kind != 'image' and name:
            names.append(name)
        names += [value for path, value in strings(local_extra or b'')
                  if re.match(r'^[A-Za-z]:[/\\]', value)]
        paths = []
        seen = set()
        for candidate_name in names:
            for path in self._find_paths(candidate_name):
                key = os.path.normcase(str(path))
                if key not in seen:
                    paths.append(path)
                    seen.add(key)
        extension = Path(str(name).replace('\\', '/')).suffix.lower()
        if not re.fullmatch(r'\.[a-z0-9]{1,10}', extension):
            extension = '.jpg' if kind == 'image' else '.bin'
        reasons = []
        fallback = None
        for path in paths:
            try:
                outcome = self._check_and_copy(path, digest, size, extension, dimensions)
            except (OSError, ValueError) as error:
                reasons.append(type(error).__name__)
                continue
            if outcome['status'] == 'ready' and (kind != 'image' or outcome['image_format'] is not None):
                if kind == 'image' and not outcome['original']:
                    if fallback is None or outcome['dimensions'][0] * outcome['dimensions'][1] > fallback['dimensions'][0] * fallback['dimensions'][1]:
                        fallback = outcome
                    continue
                attachment.update(outcome)
                if kind != 'image':
                    attachment.pop('image_format', None)
                return attachment
            reasons.append(outcome.get('reason', 'not_a_supported_image'))
        if kind == 'image' and fallback is not None:
            attachment.update(fallback)
            return attachment
        attachment['reason'] = ','.join(sorted(set(reasons))) if reasons else 'not_in_local_cache'
        return attachment
