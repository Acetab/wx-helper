"""通过配置接入 OpenAI 兼容的大模型接口，不记录密钥或聊天正文。"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class LLMError(RuntimeError):
    pass


class LLMAuthError(LLMError):
    """密钥、余额或模型配置问题：重试没有意义，直接失败。"""


class LLMFormatError(LLMError):
    """模型输出不符合约定结构：可以补救一次。"""


class LLMClient:
    def __init__(self, options):
        if not isinstance(options, dict):
            raise ValueError("大模型配置必须是 JSON 对象")
        base_url = options.get("base_url", "https://api.deepseek.com")
        if not isinstance(base_url, str):
            raise ValueError("大模型 base_url 必须是 HTTPS 地址字符串")
        self.base_url = base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("大模型 base_url 应为不含认证信息的 HTTP(S) 地址")
        if parsed.scheme == "http" and (parsed.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("非本机地址的大模型 base_url 必须使用 HTTPS")
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

    def _messages(self, system, user, history):
        if not isinstance(history, (list, tuple)):
            raise ValueError("history 应为消息列表")
        messages = [{"role": "system", "content": system}]
        for row in history:
            if (not isinstance(row, dict) or row.get("role") not in ("user", "assistant") or
                    not isinstance(row.get("content"), str) or not row["content"].strip()):
                raise ValueError("history 消息需要 user/assistant 角色和非空正文")
            messages.append({"role": row["role"], "content": row["content"]})
        messages.append({"role": "user", "content": user})
        return messages

    def _post(self, messages, budget, *, json_mode, timeout):
        payload = {"model": self.model, "messages": messages, "max_tokens": budget,
                   "thinking": {"type": "disabled"}, "stream": False}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        request = Request(self.base_url + "/chat/completions",
                          data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers={"Authorization": "Bearer " + self._api_key,
                                   "Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(1024 * 1024 + 1)
        except HTTPError as error:
            # 不输出响应正文或请求头，避免服务器错误回显密钥。
            if error.code in (401, 403):
                raise LLMAuthError(f"大模型接口 HTTP {error.code}；请核对密钥与模型配置") from None
            raise LLMError(f"大模型接口 HTTP {error.code}；请核对密钥、余额和模型配置") from None
        except (URLError, TimeoutError, OSError):
            raise LLMError("大模型连接失败或超时，请稍后再试") from None
        if len(raw) > 1024 * 1024:
            raise LLMError("大模型响应过大")
        try:
            result = json.loads(raw)
        except ValueError:
            raise LLMFormatError("大模型响应不是合法 JSON，请重试") from None
        try:
            choice = result["choices"][0]
            if choice.get("finish_reason") == "length":
                raise LLMError("大模型响应被截断，请增加 max_tokens")
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError, AttributeError):
            raise LLMFormatError("大模型响应缺少必要字段，请重试") from None
        if not isinstance(content, str) or not content.strip():
            raise LLMFormatError("大模型返回了空结果")
        return content

    def complete_json(self, system, user, *, history=(), max_tokens=None):
        budget = self.max_tokens if max_tokens is None else max_tokens
        if type(budget) is not int or not 128 <= budget <= 4096:
            raise ValueError("任务 max_tokens 应为128—4096的整数")
        messages = self._messages(system, user, history)
        deadline = time.monotonic() + self.timeout
        content = self._post(messages, budget, json_mode=True, timeout=self.timeout)
        try:
            data = json.loads(content)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return data
        # 只补救一次，且与首次调用共享总的等待预算。
        remaining = deadline - time.monotonic()
        if remaining >= 1:
            repair = [*messages, {"role": "assistant", "content": content},
                      {"role": "user", "content": "上一次输出不是合法的 JSON 对象。请只输出一个 JSON 对象，"
                                                   "不要代码块、注释或解释文字。"}]
            try:
                repaired = self._post(repair, budget, json_mode=True, timeout=remaining)
            except LLMError:
                pass
            else:
                try:
                    fixed = json.loads(repaired)
                except ValueError:
                    fixed = None
                if isinstance(fixed, dict):
                    return fixed
        raise LLMFormatError("大模型输出不是合法的 JSON 对象，已尝试补救")

    def complete_text(self, system, user, *, history=(), max_tokens=None):
        """普通聊天走纯文本输出：模型不必生成 JSON，格式错误不再影响正常对话。"""
        budget = self.max_tokens if max_tokens is None else max_tokens
        if type(budget) is not int or not 128 <= budget <= 4096:
            raise ValueError("任务 max_tokens 应为128—4096的整数")
        messages = self._messages(system, user, history)
        return self._post(messages, budget, json_mode=False, timeout=self.timeout)
