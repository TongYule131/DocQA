"""静态检查：未使用的导入与语法完整性（不依赖额外 lint 工具）。"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

TARGETS = [
    "app/rag.py", "app/rag_context.py", "app/rag_prompts.py", "app/rag_validation.py",
    "app/config.py", "app/main.py", "app/schemas.py", "app/vector_index.py",
    "scripts/evaluate_rag.py", "scripts/evaluate_rag_live.py", "scripts/check_secrets.py",
    "scripts/inspect_scan_page.py", "scripts/container_ocr_page.py",
    "tests/test_rag_context.py", "tests/test_rag_validation.py", "tests/test_rag_api.py",
    "tests/test_rag_semantic_fixtures.py",
]
# 这些名字是显式再导出或协议需要的，允许未在本文件使用。
ALLOWED = {"annotations"}
# 实施前就已存在于 app/main.py 的未使用导入（基线 d0b4843），本轮不改动既有文件风格。
PREEXISTING = {
    ("app/main.py", "Answer"), ("app/main.py", "ParseError"),
    ("app/main.py", "chunk_pages"), ("app/main.py", "parse_document"),
}


def main() -> int:
    problems = 0
    for name in TARGETS:
        path = Path(name)
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=name)
        except SyntaxError as exc:
            print(f"语法错误：{name}:{exc.lineno}: {exc.msg}")
            problems += 1
            continue
        imported: dict[str, int] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    key = alias.asname or alias.name.split(".")[0]
                    imported[key] = node.lineno
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    key = alias.asname or alias.name
                    imported[key] = node.lineno
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                current = node
                while isinstance(current, ast.Attribute):
                    current = current.value
                if isinstance(current, ast.Name):
                    used.add(current.id)
        # 字符串注解与 __all__ 中的名字也算使用。
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for key in imported:
                    if key in node.value:
                        used.add(key)
        unused = {key: line for key, line in imported.items()
                  if key not in used and key not in ALLOWED
                  and (name, key) not in PREEXISTING}
        if unused:
            for key, line in sorted(unused.items()):
                print(f"未使用的导入：{name}:{line} -> {key}")
            problems += len(unused)
    print(f"检查文件 {len(TARGETS)} 个；问题 {problems} 处")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
