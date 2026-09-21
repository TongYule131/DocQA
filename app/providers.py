"""外部能力契约。Qwen / ChatGLM / Llama 适配器应实现 LanguageModel。

框架阶段不提供伪造的模型输出或用关键词匹配冒充向量检索。
"""
from typing import Protocol

from app.schemas import Chunk, Page


class OCRProvider(Protocol):
    # 输入单页图像字节及原始页码，输出带页码的识别文本。
    def recognize(self, image: bytes, page_number: int) -> Page: ...


class EmbeddingProvider(Protocol):
    # 批量生成向量；实现方应保证向量顺序与输入文本顺序一致。
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class VectorStore(Protocol):
    # 按块 ID 新增或更新索引，chunks 与 vectors 应一一对应。
    def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None: ...

    # 在指定文档范围内检索最相关的 top_k 个块，避免混入其他文档。
    def search(self, vector: list[float], document_id: str, top_k: int) -> list[Chunk]: ...


class LanguageModel(Protocol):
    # 统一模型调用入口；具体适配器负责请求格式、认证和结果转换。
    def generate(self, system_prompt: str, user_prompt: str) -> str: ...
