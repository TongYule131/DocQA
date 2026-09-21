"""DeepSeek 在线模型适配器，实现 providers.LanguageModel 的 generate 契约。"""
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, OpenAI

from app.config import Settings


class ModelError(Exception):
    # 只携带面向用户的安全提示，不把服务商原始响应或请求头暴露给前端。
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class DeepSeekModel:
    def __init__(self, settings: Settings):
        self.settings = settings

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        if not self.settings.deepseek_api_key.strip():
            raise ModelError(503, "尚未配置 DEEPSEEK_API_KEY，请填写本地 .env 后重启服务")
        try:
            # 每次调用关闭客户端；不自动重试，避免超时后重复生成与计费。
            with OpenAI(
                api_key=self.settings.deepseek_api_key,
                base_url=self.settings.deepseek_base_url,
                timeout=self.settings.deepseek_timeout_seconds,
                max_retries=0,
            ) as client:
                response = client.chat.completions.create(
                    model=self.settings.deepseek_model,
                    messages=[{"role": "system", "content": system_prompt},
                              {"role": "user", "content": user_prompt}],
                    stream=False,
                    max_tokens=self.settings.deepseek_max_tokens,
                    reasoning_effort=self.settings.deepseek_reasoning_effort,
                    extra_body={"thinking": {"type": self.settings.deepseek_thinking}},
                )
        except APITimeoutError:
            raise ModelError(504, "DeepSeek 响应超时，请稍后重试或调整超时配置") from None
        except APIConnectionError:
            raise ModelError(502, "无法连接 DeepSeek，请检查网络和 API 基础地址") from None
        except APIStatusError as exc:
            messages = {
                400: "DeepSeek 请求参数不受支持，请检查模型和思考参数配置",
                401: "DeepSeek 密钥无效，请检查本地 DEEPSEEK_API_KEY",
                402: "DeepSeek 账户余额不足，请检查账户状态",
                403: "DeepSeek 拒绝访问，请检查模型使用权限",
                404: "DeepSeek 模型或接口不存在，请检查基础地址和模型名称",
                422: "DeepSeek 无法处理请求，请检查参数配置",
                429: "DeepSeek 请求频率受限，请稍后重试",
            }
            raise ModelError(503 if exc.status_code == 429 else 502,
                             messages.get(exc.status_code, "DeepSeek 服务暂时不可用，请稍后重试")) from None
        except (APIError, ValueError):
            raise ModelError(502, "DeepSeek 返回了无法解析的响应") from None

        # 只返回最终正文，不将思考内容当作答案，也不接受被长度限制截断的结果。
        try:
            choice = response.choices[0]
            content = choice.message.content
            complete = choice.finish_reason == "stop"
        except (AttributeError, IndexError, TypeError):
            raise ModelError(502, "DeepSeek 响应结构不完整") from None
        if not complete:
            raise ModelError(502, "DeepSeek 未返回完整答案，请检查输出长度限制或重试")
        if not isinstance(content, str) or not content.strip():
            raise ModelError(502, "DeepSeek 未返回有效答案正文")
        return content.strip()
