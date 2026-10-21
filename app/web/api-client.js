// 同源传输层。连接超时只结束网页等待，不代表服务端任务或计费被取消。
export function createApiClient(fetcher = (...args) => (globalThis.docqaFetch || fetch)(...args)) {
  return async function request(path, options = {}) {
    if (!path.startsWith('/api/')) throw new Error('请求地址不受支持');
    const { timeoutMs = 10000, ...fetchOptions } = options;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetcher(path, { ...fetchOptions, signal: controller.signal, cache: 'no-store' });
      let body = null;
      try { body = await response.json(); } catch { /* 错误页不一定是 JSON。 */ }
      if (!response.ok) {
        const error = new Error(typeof body?.detail === 'string' ? body.detail : '暂时无法处理，请稍后重试');
        error.status = response.status;
        error.supportId = response.headers?.get('X-Request-ID') || '';
        throw error;
      }
      if (body === null) throw new Error('服务返回不完整，请刷新已有记录核对');
      return body;
    } finally { clearTimeout(timer); }
  };
}

export const encoded = value => encodeURIComponent(value);
