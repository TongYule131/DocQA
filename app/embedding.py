"""OpenAI 兼容 Embedding 适配器：批量向量化、结果校验和中文错误提示。"""
import hashlib
import json
import math

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, OpenAI

from app.config import Settings


class EmbeddingError(Exception):
    # 上游响应可能包含内部信息，接口只使用这里定义的安全提示。
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def normalize_vector(vector: list[float]) -> list[float]:
    """校验并归一化向量，使后续点积等价于余弦相似度。"""
    if not isinstance(vector, list) or not vector:
        raise EmbeddingError(502, "Embedding 返回了空向量或无效格式")
    if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in vector):
        raise EmbeddingError(502, "Embedding 向量包含无效数值")
    # 先缩放再计算长度，避免极大有限数值在平方运算中溢出。
    scale = max(abs(v) for v in vector)
    if scale == 0:
        raise EmbeddingError(502, "Embedding 返回了零向量")
    scaled = [v / scale for v in vector]
    length = math.sqrt(sum(v * v for v in scaled))
    return [v / length for v in scaled]


class APIEmbedding:
    """实现 providers.EmbeddingProvider；密钥仅发送到服务端配置的网关。"""
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def signature(self) -> str:
        # 地址或模型改变时旧索引失效；密钥轮换不影响向量空间。
        identity = [self.settings.embedding_base_url.rstrip('/'), self.settings.embedding_model, 'float-unit-v1']
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.settings.embedding_api_key.strip():
            raise EmbeddingError(503, "尚未配置 EMBEDDING_API_KEY，请填写网关密钥并重启服务")
        if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise EmbeddingError(422, "待向量化文本不能为空")
        vectors = []
        dimension = None
        try:
            # 关闭自动重试，避免上游已完成但响应丢失时产生重复计费。
            with OpenAI(api_key=self.settings.embedding_api_key,
                        base_url=self.settings.embedding_base_url,
                        timeout=self.settings.embedding_timeout_seconds, max_retries=0) as client:
                for offset in range(0, len(texts), self.settings.embedding_batch_size):
                    batch = texts[offset:offset + self.settings.embedding_batch_size]
                    response = client.embeddings.create(
                        model=self.settings.embedding_model, input=batch, encoding_format="float")
                    # 按响应 index 恢复输入顺序，不能依赖服务商返回数组的排列。
                    data = response.data
                    if not isinstance(data, list) or len(data) != len(batch):
                        raise EmbeddingError(502, "Embedding 返回的向量数量与输入不一致")
                    by_index = {}
                    for item in data:
                        index = item.index
                        if type(index) is not int or index not in range(len(batch)) or index in by_index:
                            raise EmbeddingError(502, "Embedding 返回了无效或重复的文本序号")
                        vector = normalize_vector(item.embedding)
                        dimension = dimension or len(vector)
                        if len(vector) != dimension:
                            raise EmbeddingError(502, "Embedding 返回的向量维度不一致")
                        by_index[index] = vector
                    vectors.extend(by_index[i] for i in range(len(batch)))
        except APITimeoutError:
            raise EmbeddingError(504, "Embedding 网关响应超时，请稍后重试或调整超时配置") from None
        except APIConnectionError:
            raise EmbeddingError(502, "无法连接 Embedding 网关，请检查网络和基础地址") from None
        except APIStatusError as exc:
            messages = {401: "Embedding 密钥无效，请检查网关 API Key",
                        402: "Embedding 网关账户余额不足",
                        403: "Embedding 网关拒绝访问，请检查模型权限",
                        404: "Embedding 模型或接口不存在，请检查地址和模型名称",
                        429: "Embedding 请求频率受限，请稍后重试",
                        400: "Embedding 参数被拒绝，请检查模型、输入长度和批次大小",
                        413: "Embedding 输入过长，请减小批次大小或分块长度",
                        422: "Embedding 参数不受支持，请检查模型与批次配置"}
            raise EmbeddingError(503 if exc.status_code == 429 else 502,
                                 messages.get(exc.status_code, "Embedding 网关暂时不可用")) from None
        except (APIError, ValueError, AttributeError, TypeError):
            raise EmbeddingError(502, "Embedding 网关返回了无法解析的响应") from None
        return vectors
