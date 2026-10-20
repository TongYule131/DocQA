"""不可变解析版本内的引述身份；显示编号、检索顺序和答案ID不参与身份。"""
from __future__ import annotations

from hashlib import sha256
import json

OFFSET_BASIS = "chunk_crlf_lf_unicode_codepoints"
CITATION_IDENTITY_VERSION = "source-span-v1"


def citation_identity(document_id: str, parse_version_id: str, chunk_id: str,
                      char_start: int, char_end: int, quote: str) -> dict:
    if (not all(isinstance(v, str) and v for v in (document_id, parse_version_id, chunk_id, quote))
            or type(char_start) is not int or type(char_end) is not int
            or char_start < 0 or char_end - char_start != len(quote)):
        raise ValueError("invalid_citation_range")
    quote_hash = sha256(quote.encode("utf-8")).hexdigest()
    identity = [CITATION_IDENTITY_VERSION, document_id, parse_version_id, chunk_id,
                OFFSET_BASIS, char_start, char_end, quote_hash]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return dict(evidence_id="ev-" + sha256(encoded).hexdigest(), quote_sha256=quote_hash,
                char_start=char_start, char_end=char_end, offset_basis=OFFSET_BASIS,
                verification="exact_match", semantic_status="not_reviewed")
