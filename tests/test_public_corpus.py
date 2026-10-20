"""公开原件采集的固定来源与本地校验；测试禁止访问网络。"""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import zipfile

from pypdf import PdfWriter
import pytest

from scripts import collect_public_corpus as collector


def pdf_bytes():
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    writer.write(stream)
    return stream.getvalue()


def manifest_file(tmp_path, *, digest=None):
    docs = []
    for index, url in enumerate(sorted(collector.ALLOWLIST)):
        fmt = "xlsx" if url.endswith(".xlsx") else "pdf"
        docs.append({"id": f"test-{index}", "filename": f"test-{index}.{fmt}",
            "format": fmt, "download_url": url, "split": "dev" if index < 3 else "holdout",
            "source_family": f"family-{index}", "sha256": digest, "publisher": "合成测试",
            "landing_url": "https://example.invalid", "license_status": "synthetic",
            "access_conditions": "仅测试", "questions": [
                {"id": f"q-{index}-{q}", "request": "合成问题", "required_points": ["合成事实"],
                 "sources": ["page:1"], "forbidden_inferences": ["不得编造"]} for q in range(2)]})
    manifest = {"schema_version": "docqa-public-corpus-v1", "documents": docs}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, manifest


def test_default_verification_never_downloads_or_creates_missing_files(tmp_path, monkeypatch):
    path, _ = manifest_file(tmp_path)
    def forbidden(*_):
        raise AssertionError("默认禁止下载")
    monkeypatch.setattr(collector, "download_bytes", forbidden)
    destination = tmp_path / "isolated-corpus"
    result = collector.collect(destination, manifest_path=path)
    assert result["available"] == 0 and result["planned"] == 6
    assert result["model_provider_calls"] == 0
    assert result["semantic_acceptance"] == "not_executed"
    assert not destination.exists()


@pytest.mark.parametrize("url", ["https://localhost/file.pdf", "file:///tmp/a.pdf",
    "https://nvlpubs.nist.gov:444/nistpubs/ai/NIST.AI.100-1.pdf",
    "https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf#altered"])
def test_arbitrary_urls_rejected(url):
    with pytest.raises(collector.PublicCorpusError):
        collector.validate_url(url)


@pytest.mark.parametrize("change", ["duplicate_id", "duplicate_filename", "path", "split", "hash", "same_family", "annotations"])
def test_invalid_manifest_is_rejected(tmp_path, change):
    path, manifest = manifest_file(tmp_path)
    docs = manifest["documents"]
    if change == "duplicate_id": docs[1]["id"] = docs[0]["id"]
    elif change == "duplicate_filename": docs[1]["filename"] = docs[0]["filename"]
    elif change == "path": docs[0]["filename"] = "../outside.pdf"
    elif change == "split": docs[0]["split"] = "all"
    elif change == "hash": docs[0]["sha256"] = "invalid"
    elif change == "same_family": docs[3]["source_family"] = docs[0]["source_family"]
    else: docs[0]["questions"] = []
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(collector.PublicCorpusError): collector.load_sources(path)


def test_explicit_download_has_hash_receipt_and_does_not_overwrite(tmp_path, monkeypatch):
    content = pdf_bytes()
    path, manifest = manifest_file(tmp_path, digest=sha256(content).hexdigest())
    source = next(d for d in manifest["documents"] if d["format"] == "pdf")
    calls = []
    def download(url):
        calls.append(url)
        return content, {"Content-Type": "application/pdf"}
    monkeypatch.setattr(collector, "download_bytes", download)
    destination = tmp_path / "corpus"
    result = collector.collect(destination, download=True, manifest_path=path, document_ids=[source["id"]])
    assert result["available"] == 1 and calls == [source["download_url"]]
    receipt = json.loads((destination / (source["id"] + ".receipt.json")).read_text(encoding="utf-8"))
    assert receipt["sha256"] == source["sha256"] and receipt["original_unmodified"]
    assert collector.collect(destination, download=True, manifest_path=path,
        document_ids=[source["id"]])["documents"][0]["status"] == "verified_existing"
    assert len(calls) == 1
    (destination / source["filename"]).write_bytes(b"altered")
    with pytest.raises(collector.PublicCorpusError, match="禁止覆盖"):
        collector.collect(destination, download=True, manifest_path=path, document_ids=[source["id"]])
    assert len(calls) == 1


@pytest.mark.parametrize("content", [b"<html>error</html>", b"%PDF-invalid", b""])
def test_fake_pdf_never_published(tmp_path, monkeypatch, content):
    path, manifest = manifest_file(tmp_path)
    source = next(d for d in manifest["documents"] if d["format"] == "pdf")
    monkeypatch.setattr(collector, "download_bytes", lambda _: (content, {}))
    destination = tmp_path / "corpus"
    with pytest.raises(collector.PublicCorpusError):
        collector.collect(destination, download=True, manifest_path=path, document_ids=[source["id"]])
    assert not (destination / source["filename"]).exists()


def test_office_package_rejects_missing_part_and_traversal():
    for names in (("[Content_Types].xml",), ("[Content_Types].xml", "xl/workbook.xml", "../bad")):
        stream = BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for name in names: archive.writestr(name, "<test/>")
        with pytest.raises(collector.PublicCorpusError): collector.validate_content(stream.getvalue(), "xlsx")


def test_collector_rejects_formal_or_git_destination_and_unknown_id(tmp_path):
    path, _ = manifest_file(tmp_path)
    for destination in (collector.ROOT / "data", collector.ROOT / "tests" / "fixtures"):
        with pytest.raises(collector.PublicCorpusError): collector.collect(destination, manifest_path=path)
    destination = tmp_path / "database"
    destination.mkdir()
    (destination / "docqa.db").write_bytes(b"untouched")
    with pytest.raises(collector.PublicCorpusError): collector.collect(destination, manifest_path=path)
    with pytest.raises(collector.PublicCorpusError):
        collector.collect(tmp_path / "corpus", manifest_path=path, document_ids=["unknown"])


def test_checked_in_manifest_has_frozen_hashes_for_available_originals():
    manifest = collector.load_sources()
    assert len(manifest["documents"]) == 6
    assert sum(d["sha256"] is not None for d in manifest["documents"]) >= 5
