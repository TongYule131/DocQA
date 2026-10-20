"""准备18份合成资料与144项标注；无网络、无模型、无正式库操作。"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.evaluation_corpus import CorpusError, DEFAULT_CORPUS, prepare_corpus


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_CORPUS)
    args = parser.parse_args(argv)
    try:
        report = prepare_corpus(args.output_dir)
    except (CorpusError, OSError, ValueError) as error:
        print(json.dumps({"error": str(error), "provider_calls": 0}, ensure_ascii=False))
        return 2
    path = args.output_dir / "preparation-report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"documents": report["documents"], "scenarios": report["scenarios"], "provider_calls": 0,
        "semantic_acceptance": report["semantic_acceptance"], "report": str(path.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
