"""Small, strict protobuf wire reader for locally verified WeCom payloads."""
from __future__ import annotations


def _varint(data: bytes, offset: int):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError('Truncated protobuf integer')
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError('Oversized protobuf integer')


def fields(data: bytes):
    result = {}
    offset = 0
    while offset < len(data):
        tag, offset = _varint(data, offset)
        number, kind = tag >> 3, tag & 7
        if number == 0 or number >= (1 << 29):
            raise ValueError('Invalid protobuf field')
        if kind == 0:
            value, offset = _varint(data, offset)
        elif kind in (1, 5):
            size = 8 if kind == 1 else 4
            if offset + size > len(data):
                raise ValueError('Truncated fixed protobuf field')
            value = data[offset:offset + size]
            offset += size
        elif kind == 2:
            size, offset = _varint(data, offset)
            if offset + size > len(data):
                raise ValueError('Truncated protobuf bytes')
            value = data[offset:offset + size]
            offset += size
        else:
            raise ValueError('Unsupported protobuf wire type')
        result.setdefault(number, []).append((kind, value))
    return result


def blobs(parsed, number):
    return [value for kind, value in parsed.get(number, ()) if kind == 2]


def blob(parsed, number, default=b''):
    return next((value for kind, value in parsed.get(number, ()) if kind == 2), default)


def integer(parsed, number, default=0):
    return next((value for kind, value in parsed.get(number, ()) if kind == 0), default)


def text(parsed, number, default=''):
    value = blob(parsed, number)
    if not value:
        return default
    return value.decode('utf-8')


def strings(data: bytes, path=(), *, depth=0):
    if depth > 8:
        return
    try:
        parsed = fields(data)
    except ValueError:
        return
    for number, values in parsed.items():
        for kind, value in values:
            if kind != 2 or not value:
                continue
            try:
                decoded = value.decode('utf-8')
                if not all(char.isprintable() or char in '\r\n\t' for char in decoded):
                    raise ValueError('Binary protobuf child')
            except (UnicodeError, ValueError):
                yield from strings(value, path + (number,), depth=depth + 1)
            else:
                yield path + (number,), decoded
