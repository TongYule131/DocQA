"""DeepSeek 在线模型适配器，实现 providers.LanguageModel 的 generate 契约。"""
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, OpenAI
from contextvars import ContextVar
import httpx

from app.config import Settings
from app.provider_usage import response_metadata
from app.resource_governance import guarded_provider


class ModelError(Exception):
    # 只携带面向用户的安全提示，不把服务商原始响应或请求头暴露给前端。
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class DeepSeekModel:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._call_metadata = ContextVar("deepseek_call_metadata", default=None)

    @property
    def last_call_metadata(self) -> dict | None:
        # 同一适配器可被并发同步接口使用，元数据按执行上下文隔离。
        return self._call_metadata.get()

    @guarded_provider("deepseek", ModelError)
    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self._call_metadata.set(None)
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
            finish_reason = choice.finish_reason
            complete = finish_reason == "stop"
        except (AttributeError, IndexError, TypeError):
            raise ModelError(502, "DeepSeek 响应结构不完整") from None
        self._call_metadata.set(response_metadata(response, finish_reason=finish_reason))
        if not complete:
            if finish_reason == "length":
                raise ModelError(502, "DeepSeek 未返回完整答案：输出额度已耗尽；"
                                 "请调高 DEEPSEEK_MAX_TOKENS（思考与正文共用额度）后明确重试")
            raise ModelError(502, "DeepSeek 未返回完整答案，请检查输出长度限制或重试")
        if not isinstance(content, str) or not content.strip():
            raise ModelError(502, "DeepSeek 未返回有效答案正文")
        return content.strip()

    @guarded_provider("deepseek", ModelError)
    def generate_stream(self, system_prompt: str, user_prompt: str, on_text, check_active=lambda: None) -> str:
        """单次真实上游流；思考不外传，中断不降级为第二次生成。"""
        from app.answer_stream import MAX_STREAM_CHARS
        self._call_metadata.set(None)
        if not self.settings.deepseek_api_key.strip():
            raise ModelError(503, '尚未配置 DEEPSEEK_API_KEY')
        fragments=[];size=0;finish=None;metadata={}
        try:
            with OpenAI(api_key=self.settings.deepseek_api_key,base_url=self.settings.deepseek_base_url,
                        timeout=self.settings.deepseek_timeout_seconds,max_retries=0) as client:
                check_active()
                with client.chat.completions.create(model=self.settings.deepseek_model,
                    messages=[{'role':'system','content':system_prompt},{'role':'user','content':user_prompt}],
                    stream=True,stream_options={'include_usage':True},max_tokens=self.settings.deepseek_max_tokens,
                    reasoning_effort=self.settings.deepseek_reasoning_effort,
                    extra_body={'thinking':{'type':self.settings.deepseek_thinking}}) as stream:
                    if not stream.response.headers.get('content-type','').lower().startswith('text/event-stream'):
                        raise ModelError(502,'上游未返回流式响应；未降级为重复生成')
                    for chunk in stream:
                        check_active()
                        current=response_metadata(chunk,finish_reason=finish)
                        if current.get('usage'):metadata.update(current)
                        elif current.get('provider_request_id'):metadata['provider_request_id']=current['provider_request_id']
                        choices=getattr(chunk,'choices',[])
                        if not choices:continue
                        if len(choices)!=1 or choices[0].index!=0:raise ModelError(502,'流式响应结构无效')
                        choice=choices[0];text=choice.delta.content
                        if text:
                            if finish is not None or not isinstance(text,str):raise ModelError(502,'流式响应顺序无效')
                            size+=len(text)
                            if size>MAX_STREAM_CHARS:raise ModelError(502,'流式响应超出受控长度')
                            fragments.append(text);on_text(text)
                        if choice.finish_reason is not None:
                            if finish is not None:raise ModelError(502,'流式结束标识重复')
                            finish=choice.finish_reason;metadata['finish_reason']=finish
                    request_id=stream.response.headers.get('x-request-id')
                    if request_id and len(request_id)<=200 and all(c.isalnum() or c in '-_.:' for c in request_id):
                        metadata['provider_request_id']=request_id
                check_active()
        except (APITimeoutError,APIConnectionError,httpx.TransportError):
            raise ModelError(504,'流式连接中断，调用结果不确定；系统不会自动重发') from None
        except APIStatusError as exc:
            raise ModelError(503 if exc.status_code==429 else 502,
                             '流式请求被上游拒绝，请检查模型、网关及流式参数；未自动降级重发') from None
        except APIError:
            raise ModelError(504,'上游流式连接未正常结束；系统不会自动重发') from None
        finally:
            metadata.setdefault('usage',None);metadata.setdefault('usage_status','unknown')
            self._call_metadata.set(metadata)
        if finish!='stop':
            raise ModelError(502 if finish in ('length','content_filter','tool_calls','function_call') else 504,
                             '流式答案未完整结束；已接收内容不作为最终回答，未自动重发')
        text=''.join(fragments).strip()
        if not text:raise ModelError(502,'上游未返回有效答案正文')
        return text
