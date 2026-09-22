# Pydantic 数据契约：用于请求校验、响应序列化和接口文档生成。
from typing import Literal

from pydantic import BaseModel, Field


class Page(BaseModel):
    # PDF 页码从 1 开始；TXT 作为单个逻辑页处理。
    number: int
    text: str


class Chunk(BaseModel):
    # 保存原文与来源关联，后续向量索引和回答引用都使用该块 ID。
    id: str
    document_id: str
    page: int
    text: str


class Document(BaseModel):
    # 文档元数据；size 为字节数，created_at 为 UTC 时间字符串。
    id: str
    filename: str
    size: int
    created_at: str
    # parsed 仅表示文本解析完成，不表示已向量化或可以进行智能问答。
    status: Literal["uploaded", "parsing", "parsed", "failed"]
    page_count: int = 0
    chunk_count: int = 0
    error: str | None = None


class Question(BaseModel):
    # 限制问题长度，并要求至少包含一个非空白字符。
    question: str = Field(min_length=1, max_length=4000, pattern=r"\S")


class SearchRequest(BaseModel):
    # 只检索当前文档，限制返回数量，避免一次请求取回过多原文。
    query: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    top_k: int = Field(default=5, ge=1, le=20)


class Citation(BaseModel):
    # 来源包含文档、分块、页码及引用文本；真实性需由后续业务层校验。
    document_id: str
    chunk_id: str
    page: int
    quote: str


class Answer(BaseModel):
    # 问答响应契约：正文和支撑答案的来源列表。
    answer: str
    citations: list[Citation]


class Summary(BaseModel):
    # 摘要也保留引用，方便对照原文核实。
    summary: str
    citations: list[Citation]


class ExtractedItem(BaseModel):
    # 三类关键信息分别对应数据、结论、观点。
    kind: Literal["data", "conclusion", "viewpoint"]
    content: str
    citations: list[Citation]


class Extraction(BaseModel):
    # 一次提取可以返回多个条目，每个条目拥有自己的引用。
    items: list[ExtractedItem]
