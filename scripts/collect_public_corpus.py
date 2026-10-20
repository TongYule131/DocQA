"""公开原件采集储备：默认只读本地，只有显式 --download 才访问固定官方URL。

采集不是Docling解析或供应商语义验收；原件和下载凭证仅保存到隔离data目录。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_MANIFEST = ROOT / "tests" / "fixtures" / "improvement" / "public_sources.json"
DEFAULT_OUTPUT = ROOT / "data" / "improvement-20261018" / "public-corpus"
MAX_BYTES = 64 * 1024 * 1024
# 网络边界独立于可替换的JSON参数：不允许任意URL、协议、端口或跨站重定向。
ALLOWLIST = {
    # 教育部此公开附件HTTPS端点重定向HTTP；仅允许已核对的该精确HTTP URL。
    "http://www.moe.gov.cn/srcsite/A26/s8001/202204/W020220420582361024968.pdf",
    "https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf",
    "https://assets.publishing.service.gov.uk/media/6aba724dfceb6fb3a65012d2/ET_6.1_SEP_26.xlsx",
    "https://www.mee.gov.cn/hjzl/sthjzk/zghjzkgb/202506/P020250604527010717462.pdf",
    "https://science.nasa.gov/wp-content/uploads/2023/04/fy_22_strategic_plan-1.pdf",
    "https://www.shanwei.gov.cn/attachment/0/48/48927/937499.pdf",
}


class PublicCorpusError(ValueError):
    pass


def validate_url(url: str) -> None:
    part = urlsplit(url)
    if url not in ALLOWLIST or part.scheme not in {"https", "http"} or part.port not in (None, 443, 80) or part.username or part.password or part.fragment:
        raise PublicCorpusError("URL不在固定官方原件白名单")


def load_sources(path: Path = SOURCE_MANIFEST) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "docqa-public-corpus-v1" or len(manifest.get("documents", [])) != 6:
        raise PublicCorpusError("公开资料清单应包含6份原件")
    ids, filenames, groups = set(), set(), {}
    for source in manifest["documents"]:
        validate_url(source["download_url"])
        if source["id"] in ids or source["split"] not in {"dev", "holdout"}:
            raise PublicCorpusError("编号重复或分组无效")
        ids.add(source["id"])
        family = source["source_family"]
        if family in groups and groups[family] != source["split"]:
            raise PublicCorpusError("同源资料不能跨开发与holdout")
        groups[family] = source["split"]
        if not re.fullmatch(r"[a-z0-9-]+\.(pdf|xlsx|docx|txt)", source["filename"]):
            raise PublicCorpusError("原件文件名非法")
        if source["filename"] in filenames:
            raise PublicCorpusError("原件文件名重复")
        filenames.add(source["filename"])
        if source["format"] != source["filename"].rsplit(".", 1)[-1]:
            raise PublicCorpusError("文件名与格式不一致")
        if source["sha256"] is not None and not re.fullmatch(r"[a-f0-9]{64}", source["sha256"]):
            raise PublicCorpusError("SHA-256格式错误")
        if not source.get("publisher") or not source.get("landing_url") or not source.get("license_status") or not source.get("access_conditions"):
            raise PublicCorpusError("来源、授权或访问条件不完整")
        if len(source.get("questions", [])) < 2:
            raise PublicCorpusError("每份至少2项预标注")
        for case in source["questions"]:
            if not all(case.get(key) for key in ("id", "request", "required_points", "sources", "forbidden_inferences")):
                raise PublicCorpusError("题目标注不完整")
    return manifest


def validate_content(content: bytes, fmt: str) -> dict:
    if not content or len(content) > MAX_BYTES:
        raise PublicCorpusError("原件为空或超过64MiB上限")
    if fmt == "pdf":
        if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-4096:]:
            raise PublicCorpusError("下载内容不是完整PDF")
        from pypdf import PdfReader
        try:
            reader = PdfReader(BytesIO(content))
            if reader.is_encrypted or len(reader.pages) < 1:
                raise PublicCorpusError("不接受加密或空白PDF包")
            return {"format": "pdf", "pages": len(reader.pages)}
        except PublicCorpusError:
            raise
        except Exception as error:
            raise PublicCorpusError("PDF结构无效") from error
    if fmt in {"xlsx", "docx"}:
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                infos = archive.infolist()
                if sum(i.file_size for i in infos) > 128 * 1024 * 1024 or any(i.flag_bits & 1 for i in infos):
                    raise PublicCorpusError("Office包过大或已加密")
                names = set(archive.namelist())
                needed = "xl/workbook.xml" if fmt == "xlsx" else "word/document.xml"
                if "[Content_Types].xml" not in names or needed not in names:
                    raise PublicCorpusError("Office文件缺少真实格式主部件")
                if any(name.startswith("/") or ".." in Path(name).parts for name in names):
                    raise PublicCorpusError("Office包含越界路径")
                if archive.testzip() is not None:
                    raise PublicCorpusError("Office包CRC错误")
                return {"format": fmt, "package_parts": len(infos)}
        except (zipfile.BadZipFile, KeyError) as error:
            raise PublicCorpusError("下载内容不是Office文件") from error
    if fmt == "txt":
        try:
            text = content.decode("utf-8-sig")
        except UnicodeError as error:
            raise PublicCorpusError("TXT不是UTF-8") from error
        if "\x00" in text or text.lstrip().lower().startswith(("<!doctype html", "<html")):
            raise PublicCorpusError("TXT实际为二进制或HTML错误页")
        return {"format": "txt", "characters": len(text)}
    raise PublicCorpusError("不支持的原件格式")


def download_bytes(url: str) -> tuple[bytes, dict]:
    validate_url(url)
    import httpx
    with httpx.Client(timeout=httpx.Timeout(60, connect=20), trust_env=False, follow_redirects=False) as client:
        with client.stream("GET", url, headers={"User-Agent": "DocQA-local-evaluation-corpus/1.0", "Accept-Encoding": "identity"}) as response:
            if response.status_code != 200 or str(response.url) != url:
                raise PublicCorpusError(f"官方附件不可直接访问，HTTP {response.status_code}；不自动重定向")
            try:
                length = int(response.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise PublicCorpusError("无效Content-Length") from error
            if length > MAX_BYTES:
                raise PublicCorpusError("远端文件超过大小限制")
            pieces, size = [], 0
            for piece in response.iter_bytes(64 * 1024):
                size += len(piece)
                if size > MAX_BYTES:
                    raise PublicCorpusError("流式下载超过大小限制")
                pieces.append(piece)
            return b"".join(pieces), {key: response.headers[key] for key in ("Content-Type", "ETag", "Last-Modified") if key in response.headers}


def collect(destination: Path = DEFAULT_OUTPUT, *, download: bool = False, manifest_path: Path = SOURCE_MANIFEST, document_ids: list[str] | None = None) -> dict:
    manifest = load_sources(manifest_path)
    destination = Path(destination).resolve()
    if destination == (ROOT / "data").resolve() or destination.is_relative_to((ROOT / "tests").resolve()):
        raise PublicCorpusError("必须保存到独立data或临时目录，禁止写入Git夹具")
    if any(destination.glob("*.db")) or any(destination.glob("*.sqlite*")):
        raise PublicCorpusError("不能写入正式数据库目录")
    rows = []
    selected = [source for source in manifest["documents"] if document_ids is None or source["id"] in document_ids]
    if not selected or (document_ids is not None and set(document_ids) != {s["id"] for s in selected}):
        raise PublicCorpusError("所选公开资料编号不存在")
    for source in selected:
        path = destination / source["filename"]
        if path.is_symlink() or path.resolve().parent != destination:
            raise PublicCorpusError("原件目标不能经过符号链接")
        receipt_path = destination / (source["id"] + ".receipt.json")
        if path.exists():
            content = path.read_bytes()
            digest = sha256(content).hexdigest()
            if digest != source["sha256"]:
                raise PublicCorpusError("已存在原件哈希不符，禁止覆盖")
            check = validate_content(content, source["format"])
            rows.append({"id": source["id"], "status": "verified_existing", "sha256": digest, **check})
            continue
        if not download:
            rows.append({"id": source["id"], "status": "missing_not_downloaded"})
            continue
        try:
            content, headers = download_bytes(source["download_url"])
        except PublicCorpusError:
            raise
        except Exception as error:
            # 公开采集也不输出任意HTTP异常正文，错误不代表下载已成功。
            raise PublicCorpusError(f"公开原件获取失败：{source['id']}，{type(error).__name__}") from error
        check = validate_content(content, source["format"])
        digest = sha256(content).hexdigest()
        if source["sha256"] is not None and digest != source["sha256"]:
            raise PublicCorpusError("远端原件已变化，禁止替换固定版本")
        receipt = {"id": source["id"], "url": source["download_url"], "sha256": digest,
            "downloaded_at_utc": datetime.now(timezone.utc).isoformat(), "bytes": len(content),
            "https_transport": source["download_url"].startswith("https://"),
            "headers": headers, "original_unmodified": True, **check}
        destination.mkdir(parents=True, exist_ok=True)
        # 独占创建避免并发写覆盖。下载失败不发布原件；收据独立保留下载来源。
        with path.open("xb") as stream:
            stream.write(content)
        with receipt_path.open("x", encoding="utf-8") as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        rows.append({"id": source["id"], "status": "downloaded", "sha256": digest, **check})
    return {"mode": "explicit_official_download" if download else "local_only_verification",
        "documents": rows, "planned": len(selected), "available": sum(r["status"] != "missing_not_downloaded" for r in rows),
        "preannotated_questions": sum(len(d["questions"]) for d in selected),
        "model_provider_calls": 0, "docling_parsing": "not_executed", "semantic_acceptance": "not_executed",
        "manifest_sha256": sha256(manifest_path.read_bytes()).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="显式允许访问清单固定官方URL，绝不调用模型")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--document", action="append", help="仅处理指定清单编号，可重复；不改变冻结清单")
    parser.add_argument("--report", type=Path, help="新报告路径，不覆盖旧报告")
    args = parser.parse_args(argv)
    try:
        result = collect(args.output_dir, download=args.download, document_ids=args.document)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            with args.report.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
    except (PublicCorpusError, OSError, ValueError) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error), "model_provider_calls": 0}, ensure_ascii=False))
        return 2
    print(json.dumps({key: value for key, value in result.items() if key != "documents"}, ensure_ascii=False))
    return 0 if result["available"] == result["planned"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
