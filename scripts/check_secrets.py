"""密钥自检：确认本地 .env 中的密钥值没有出现在任何 Git 可见文件中。

做法：只在内存中读取 .env 的密钥值，逐个在 Git 跟踪与未跟踪（非忽略）文件中查找；
命中时只输出“哪个文件 + 哪个键”，**不输出密钥值本身**。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
SENSITIVE_KEYS = ["DEEPSEEK_API_KEY", "EMBEDDING_API_KEY"]


def git_visible_files() -> list[Path]:
    """列出 Git 可见文件（已跟踪 + 未跟踪但未被忽略）。"""
    # -z 避免中文文件名被 Git 转义后漏扫，也能正确处理带换行的文件名。
    output = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                            cwd=ROOT, capture_output=True, check=True)
    return [ROOT / name.decode("utf-8") for name in output.stdout.split(b"\0") if name]


def main() -> int:
    env_path = ROOT / ".env"
    if not env_path.exists():
        print("未找到 .env：跳过密钥比对（不影响结论，但请确认本地配置位置）")
        return 0
    values = {key: (dotenv_values(env_path, encoding="utf-8-sig").get(key) or "").strip()
              for key in SENSITIVE_KEYS}
    secrets = {key: value for key, value in values.items() if len(value) >= 8}
    if not secrets:
        print("本地 .env 中未读取到可比较的密钥长度（未输出任何值）")
        return 0

    hits = []
    scanned = 0
    for path in git_visible_files():
        if not path.is_file() or path.name == ".env":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        for key, value in secrets.items():
            if value in text:
                hits.append((str(path.relative_to(ROOT)), key))

    print(f"扫描 Git 可见文件 {scanned} 个；比较的密钥键：{', '.join(secrets)}（长度不输出）")
    if hits:
        for name, key in hits:
            print(f"命中：{name} 包含 {key} 的值 —— 必须移除后再提交")
        return 1
    print("结果：没有任何 Git 可见文件包含本地密钥值")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
