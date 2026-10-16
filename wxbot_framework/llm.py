"""通过配置接入 OpenAI 兼容的大模型接口，不记录密钥或聊天正文。"""
from __future__ import annotations

import json
import math
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, options):
        if not isinstance(options, dict):
            raise ValueError("大模型配置必须是 JSON 对象")
        base_url = options.get("base_url", "https://api.deepseek.com")
        if not isinstance(base_url, str):
            raise ValueError("大模型 base_url 必须是 HTTPS 地址字符串")
        self.base_url = base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("大模型 base_url 应为不含认证信息的 HTTPS 地址")
        self.model = options.get("model", "deepseek-flash")
        self._api_key = options.get("api_key", "")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("大模型 model 不能为空")
        if not isinstance(self._api_key, str) or not self._api_key.strip():
            raise ValueError("请在大模型配置文件中填写 api_key")
        self.timeout = options.get("timeout_seconds", 20)
        self.max_tokens = options.get("max_tokens", 512)
        if type(self.timeout) not in (int, float) or not math.isfinite(self.timeout) or not 1 <= self.timeout <= 60:
            raise ValueError("timeout_seconds 应为 1–60 秒")
        if type(self.max_tokens) is not int or not 128 <= self.max_tokens <= 4096:
            raise ValueError("max_tokens 应为 128–4096 的整数")

    @classmethod
    def from_file(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8-sig")))

    def complete_json(self, system, user, *, max_tokens=None):
        budget = self.max_tokens if max_tokens is None else max_tokens
        if type(budget) is not int or not 128 <= budget <= 4096:
            raise ValueError("任务 max_tokens 应为128—4096的整数")
        payload = {"model": self.model, "messages": [{"role": "system", "content": system},
                                                       {"role": "user", "content": user}],
                   "response_format": {"type": "json_object"}, "max_tokens": budget,
                   "thinking": {"type": "disabled"}, "stream": False}
        request = Request(self.base_url + "/chat/completions",
                          data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers={"Authorization": "Bearer " + self._api_key,
                                   "Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise LLMError("大模型响应过大")
            result = json.loads(raw)
            choice = result["choices"][0]
            if choice.get("finish_reason") == "length":
                raise LLMError("大模型响应被截断，请增加 max_tokens")
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise LLMError("大模型返回了空结果")
            data = json.loads(content)
            if not isinstance(data, dict):
                raise LLMError("大模型结果应为 JSON 对象")
            return data
        except HTTPError as error:
            # 不输出响应正文或请求头，避免服务器错误回显密钥。
            raise LLMError(f"大模型接口 HTTP {error.code}；请核对密钥、余额和模型配置") from None
        except (URLError, TimeoutError, OSError):
            raise LLMError("大模型连接失败或超时，请稍后再试") from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("大模型响应格式不正确，请重试") from None
