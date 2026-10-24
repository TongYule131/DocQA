"""只缓存已校验的不可变向量解码。每轮仍核对权限、索引、原文和向量字节。"""
from collections import OrderedDict
from hashlib import sha256
import json
from threading import RLock

from app.embedding import normalize_vector


class VectorCache:
    def __init__(self):
        self.entries = OrderedDict()
        self.size = 0
        self.lock = RLock()

    def decode(self, identity, rows, dimension, max_bytes):
        signature = sha256()
        for identifier, raw in sorted(rows):
            signature.update(identifier.encode());signature.update(b'\0');signature.update(raw.encode());signature.update(b'\0')
        key = (*identity, dimension, signature.hexdigest())
        with self.lock:
            while self.size > max_bytes and self.entries:
                _, (_, size) = self.entries.popitem(last=False)
                self.size -= size
            if max_bytes and key in self.entries:
                value, _ = self.entries.pop(key)
                self.entries[key] = (value, sum(len(v) * 32 + 128 for v in value.values()))
                return value, True
        value = {}
        for identifier, raw in rows:
            vector = tuple(normalize_vector(json.loads(raw)))
            if len(vector) != dimension:
                raise ValueError('dimension_mismatch')
            value[identifier] = vector
        size = sum(len(v) * 32 + 128 for v in value.values())
        if 0 < size <= max_bytes:
            with self.lock:
                while self.entries and self.size + size > max_bytes:
                    _, (_, old_size) = self.entries.popitem(last=False)
                    self.size -= old_size
                old = self.entries.pop(key, None)
                if old:
                    self.size -= old[1]
                self.entries[key] = (value, size)
                self.size += size
        return value, False


VECTOR_CACHE = VectorCache()
