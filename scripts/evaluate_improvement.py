"""离线清单核验及保存的复核记录记账。此入口没有收费或自动judge模式。"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.evaluation_corpus import CorpusError, DEFAULT_CORPUS, evaluate_saved_reviews, load_manifest, verify_document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--split", choices=("dev", "holdout", "all"), default="dev")
    parser.add_argument("--reviews", type=Path, help="人工复核JSON列表；没有此参数时所有语义任务均为未执行")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.corpus_dir)
        for doc in manifest["documents"]:
            verify_document(args.corpus_dir, doc)
        reviews = json.loads(args.reviews.read_text(encoding="utf-8")) if args.reviews else []
        report = evaluate_saved_reviews(manifest, reviews, split=args.split)
        target = args.report or args.corpus_dir / f"evaluation-{args.split}.json"
        if target.exists():
            raise CorpusError("报告已存在，请使用新路径保留首次结果")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (CorpusError, OSError, ValueError) as error:
        print(json.dumps({"error": str(error), "provider_calls_this_run": 0}, ensure_ascii=False))
        return 2
    print(json.dumps({"overall": report["overall"], "provider_calls_this_run": 0, "report": str(target.resolve())}, ensure_ascii=False))
    return 1 if report["overall"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
