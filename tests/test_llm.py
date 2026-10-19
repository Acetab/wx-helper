"""大模型协议和错误处理验证；使用假响应，不消耗实际 API。"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from wxbot_framework.llm import LLMClient, LLMError
from plugins.idiom_chain.judge import IdiomJudge


class JSONClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        return self.result


class LLMTests(unittest.TestCase):
    def setUp(self):
        self.options = {"api_key": "dummy-test-key", "model": "deepseek-flash"}
        self.client = LLMClient(self.options)

    def response(self, content, **extra):
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}, **extra}]}).encode())

    def test_official_json_request_and_utf8(self):
        with patch("wxbot_framework.llm.urlopen", return_value=self.response('{"idiom":"一心一意"}')) as call:
            self.assertEqual(self.client.complete_json("只返回 JSON", "中文"), {"idiom": "一心一意"})
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer dummy-test-key")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["messages"][1]["content"], "中文")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(call.call_args.kwargs["timeout"], 20)

    def test_config_from_utf8_bom_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "llm.json"
            path.write_text(json.dumps(self.options), encoding="utf-8-sig")
            self.assertEqual(LLMClient.from_file(path).model, "deepseek-flash")

    def test_loopback_http_allowed(self):
        for base_url in ("http://127.0.0.1:3425/v1", "http://localhost:3425/v1"):
            with self.subTest(base_url=base_url):
                self.assertEqual(LLMClient({**self.options, "base_url": base_url}).base_url, base_url)

    def test_remote_http_still_rejected(self):
        for base_url in ("http://example.com", "http://192.168.1.10:8000/v1"):
            with self.subTest(base_url=base_url), self.assertRaises(ValueError):
                LLMClient({**self.options, "base_url": base_url})

    def test_invalid_configuration(self):
        for change in ({"api_key": ""}, {"model": 1}, {"base_url": None},
                       {"base_url": "http://example.com"}, {"base_url": "https://user:pass@example.com"},
                       {"timeout_seconds": True}, {"timeout_seconds": float("nan")},
                       {"max_tokens": 1}, {"max_tokens": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                LLMClient({**self.options, **change})

    def test_server_and_connection_errors_hide_sensitive_details(self):
        for error in (HTTPError("https://example.com", 401, "dummy-test-key", {}, None),
                      URLError("dummy-test-key"), TimeoutError("dummy-test-key")):
            with self.subTest(error=type(error).__name__), patch("wxbot_framework.llm.urlopen", side_effect=error):
                with self.assertRaises(LLMError) as caught:
                    self.client.complete_json("JSON", "测试")
                self.assertNotIn("dummy-test-key", str(caught.exception))

    def test_malformed_or_truncated_model_results(self):
        for content in ("", "[]", None):
            with (self.subTest(content=content),
                  patch("wxbot_framework.llm.urlopen", side_effect=lambda *a, _c=content, **k: self.response(_c))):
                with self.assertRaises(LLMError):
                    self.client.complete_json("JSON", "测试")
        with patch("wxbot_framework.llm.urlopen", side_effect=lambda *a, _c="not json", **k: self.response(_c)) as call:
            with self.assertRaisesRegex(LLMError, "补救"):
                self.client.complete_json("JSON", "测试")
        self.assertEqual(call.call_count, 2)
        with patch("wxbot_framework.llm.urlopen", return_value=self.response("{}", finish_reason="length")):
            with self.assertRaisesRegex(LLMError, "截断"):
                self.client.complete_json("JSON", "测试")
        with patch("wxbot_framework.llm.urlopen", return_value=io.BytesIO(b'{"choices":[null]}')):
            with self.assertRaises(LLMError):
                self.client.complete_json("JSON", "测试")

    def test_broken_json_is_repaired_once_within_the_same_budget(self):
        responses = [self.response("这不是 JSON"), self.response('{"reply":"好了"}')]
        with patch("wxbot_framework.llm.urlopen", side_effect=responses) as call:
            self.assertEqual(self.client.complete_json("JSON", "测试"), {"reply": "好了"})
        self.assertEqual(call.call_count, 2)
        repair = json.loads(call.call_args.args[0].data)["messages"]
        self.assertEqual([row["role"] for row in repair[-2:]], ["assistant", "user"])

    def test_broken_json_fails_after_a_single_repair(self):
        with patch("wxbot_framework.llm.urlopen",
                   side_effect=lambda *a, **k: self.response("还不是 JSON")) as call:
            with self.assertRaisesRegex(LLMError, "补救"):
                self.client.complete_json("JSON", "测试")
        self.assertEqual(call.call_count, 2)

    def test_auth_errors_are_not_retried(self):
        error = HTTPError("https://example.com", 401, "dummy-test-key", {}, None)
        with patch("wxbot_framework.llm.urlopen", side_effect=error) as call:
            with self.assertRaisesRegex(LLMError, "密钥"):
                self.client.complete_json("JSON", "测试")
        self.assertEqual(call.call_count, 1)

    def test_text_completion_needs_no_json_and_carries_real_roles(self):
        with patch("wxbot_framework.llm.urlopen", return_value=self.response("直接说话的正文")) as call:
            result = self.client.complete_text("系统", "当前", history=[{"role": "user", "content": "上一问"},
                                                                    {"role": "assistant", "content": "上一答"}])
        self.assertEqual(result, "直接说话的正文")
        payload = json.loads(call.call_args.args[0].data)
        self.assertNotIn("response_format", payload)
        self.assertEqual([row["role"] for row in payload["messages"]],
                         ["system", "user", "assistant", "user"])


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.client = JSONClient({"is_idiom": True, "reason": "常见成语", "next_idiom": "发扬光大"})
        self.judge = IdiomJudge(self.client)

    def test_local_rejections_do_not_call_model(self):
        for previous, word, used in (("一心一意", "不是四字成语", ()),
                                     ("一心一意", "异想天开", ()),
                                     ("一心一意", "意气风发", ("意气风发",))):
            self.assertFalse(self.judge.evaluate(previous, word, used).accepted)
        self.assertEqual(self.client.calls, [])

    def test_success_sends_only_game_words(self):
        result = self.judge.evaluate("一心一意", "意气风发", ("一心一意",))
        self.assertTrue(result.accepted)
        self.assertEqual(result.next_idiom, "发扬光大")
        self.assertEqual(json.loads(self.client.calls[0][1]),
                         {"previous": "一心一意", "candidate": "意气风发", "used": ["一心一意", "意气风发"]})

    def test_model_can_reject_fake_four_character_word(self):
        self.client.result = {"is_idiom": False, "reason": "不是成语", "next_idiom": ""}
        result = self.judge.evaluate("一心一意", "意意意意", ("一心一意",))
        self.assertFalse(result.accepted)

    def test_invalid_model_next_word_keeps_the_accepted_player_answer(self):
        for word in ("假的", "一心一意", "发扬光大", "意气风发"):
            self.client.result["next_idiom"] = word
            self.client.calls.clear()
            with self.subTest(word=word):
                result = self.judge.evaluate("一心一意", "意气风发", ("一心一意", "发扬光大"))
                self.assertTrue(result.accepted)
                self.assertEqual(result.next_idiom, "")
                self.assertEqual(len(self.client.calls), 2)

    def test_invalid_next_word_is_corrected_without_rejudging_player(self):
        self.client.complete_json = Mock(side_effect=[
            {"is_idiom": True, "reason": "常见成语", "next_idiom": "可歌可泣"},
            {"next_idiom": "声东击西"}])
        used = ("万事如意", "意有所指", "指日可待", "待人接物", "物是人非", "非同小可", "可歌可泣")
        result = self.judge.evaluate("可歌可泣", "泣不成声", used)
        self.assertTrue(result.accepted)
        self.assertEqual(result.next_idiom, "声东击西")
        correction = json.loads(self.client.complete_json.call_args.args[1])
        self.assertEqual(correction["required_first"], "声")
        self.assertEqual(correction["used"], [*used, "泣不成声"])
        self.assertEqual(correction["rejected_next"], "可歌可泣")

    def test_next_word_request_failure_does_not_undo_an_accepted_answer(self):
        self.client.complete_json = Mock(side_effect=[
            {"is_idiom": True, "reason": "常见成语", "next_idiom": "假的"}, LLMError("测试超时")])
        result = self.judge.evaluate("一心一意", "意气风发", ("一心一意",))
        self.assertTrue(result.accepted)
        self.assertEqual(result.next_idiom, "")

    def test_malformed_next_word_correction_does_not_undo_an_accepted_answer(self):
        self.client.complete_json = Mock(side_effect=[
            {"is_idiom": True, "reason": "常见成语", "next_idiom": "假的"}, None])
        result = self.judge.evaluate("一心一意", "意气风发", ("一心一意",))
        self.assertTrue(result.accepted)
        self.assertEqual(result.next_idiom, "")

    def test_model_boolean_is_strict(self):
        self.client.result["is_idiom"] = "true"
        with self.assertRaises(LLMError):
            self.judge.evaluate("一心一意", "意气风发", ())

    def test_opening_shape(self):
        self.client.result = {"idiom": "一心一意"}
        self.assertEqual(self.judge.opening(), "一心一意")
        self.client.result = {"idiom": "假词"}
        with self.assertRaises(LLMError):
            self.judge.opening()
