import json
import logging
import time

import requests as http_requests

logger = logging.getLogger(__name__)


def _safe_int(value, default, min_value=None, max_value=None):
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if min_value is not None:
        result = max(min_value, result)
    if max_value is not None:
        result = min(max_value, result)
    return result


def _safe_float(value, default, min_value=None, max_value=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if min_value is not None:
        result = max(min_value, result)
    if max_value is not None:
        result = min(max_value, result)
    return result


class LLMService:
    """
    最终版 LLM 调用服务：
    - 统一 API 调用
    - 兼容流式 / 非流式
    - 统一提取文本
    - 重试
    - 空响应判失败
    """

    def __init__(self):
        self._session = http_requests.Session()

    # ==================== 内容提取 ====================

    def extract_text(self, result: dict) -> str:
        try:
            if not isinstance(result, dict):
                return ""

            choices = result.get("choices") or []
            if not choices:
                return ""

            first = choices[0] or {}

            message = first.get("message") or {}
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content

            delta = first.get("delta") or {}
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str):
                    return content

            text = first.get("text")
            if isinstance(text, str):
                return text

            content = first.get("content")
            if isinstance(content, str):
                return content

            return ""
        except Exception:
            return ""

    # ==================== 请求构建 ====================

    def build_payload(self, config: dict, messages: list, stream: bool = False):
        model = config.get("model", "gpt-5.4")
        temperature = _safe_float(config.get("temperature", 0.7), 0.7)
        top_p = _safe_float(config.get("topP", 0.65), 0.65)
        max_tokens = _safe_int(config.get("maxOutputTokens", 1000000), 1000000, min_value=1)

        return {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "stream": stream,
        }

    def build_headers(self, config: dict):
        headers = {"Content-Type": "application/json"}
        api_key = (config.get("apiKey") or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def build_url(self, config: dict):
        api_host = (config.get("apiHost") or "").strip().rstrip("/")
        if not api_host:
            raise ValueError("未配置 apiHost")
        return f"{api_host}/chat/completions"

    # ==================== 调用 ====================

    def call_once(self, config: dict, messages: list, use_stream: bool = True):
        try:
            url = self.build_url(config)
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "result": None,
                "text": "",
            }

        headers = self.build_headers(config)
        payload = self.build_payload(config, messages, stream=use_stream)

        timeout = 1800 if use_stream else 600

        try:
            if use_stream:
                resp = self._session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                    stream=True
                )

                if resp.status_code != 200:
                    return {
                        "success": False,
                        "error": f"API错误: {resp.status_code} - {resp.text[:300]}",
                        "result": None,
                        "text": "",
                    }

                full_content = ""

                for line in resp.iter_lines():
                    if not line:
                        continue

                    try:
                        decoded = line.decode("utf-8", errors="ignore")
                    except Exception:
                        continue

                    if not decoded.startswith("data: "):
                        continue

                    raw = decoded[6:]
                    if raw == "[DONE]":
                        break

                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue

                    first = choices[0] or {}

                    delta = first.get("delta") or {}
                    if isinstance(delta, dict):
                        delta_content = delta.get("content")
                        if isinstance(delta_content, str):
                            full_content += delta_content
                            continue

                    message = first.get("message") or {}
                    if isinstance(message, dict):
                        message_content = message.get("content")
                        if isinstance(message_content, str):
                            full_content += message_content
                            continue

                    text = first.get("text")
                    if isinstance(text, str):
                        full_content += text
                        continue

                logger.info(f"流式LLM返回完成，full_content_len={len(full_content)}")

                result = {"choices": [{"message": {"content": full_content}}]}
                return {
                    "success": bool(full_content.strip()),
                    "error": "" if full_content.strip() else "模型返回空内容",
                    "result": result,
                    "text": full_content,
                }

            else:
                resp = self._session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=timeout
                )

                if resp.status_code != 200:
                    return {
                        "success": False,
                        "error": f"API错误: {resp.status_code} - {resp.text[:300]}",
                        "result": None,
                        "text": "",
                    }

                data = resp.json()
                text = self.extract_text(data)

                return {
                    "success": bool((text or "").strip()),
                    "error": "" if (text or "").strip() else "模型返回空内容",
                    "result": data,
                    "text": text or "",
                }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "result": None,
                "text": "",
            }


    def call_with_retry(self, config: dict, messages: list, max_retries: int = 3, delay_min: int = 15, delay_max: int = 45, cancel_check=None):
        max_retries = _safe_int(max_retries, 3, min_value=1, max_value=10)
        delay_min = _safe_int(delay_min, 15, min_value=0)
        delay_max = _safe_int(delay_max, 45, min_value=delay_min)

        last = None

        for attempt in range(1, max_retries + 1):
            # 临时验证方案：批处理改为非流式，排查 stream 链路问题
            logger.info(f"批处理调用LLM（非流式验证模式）: 第 {attempt}/{max_retries} 次")
            result = self.call_once(config=config, messages=messages, use_stream=False)

            if result["success"]:
                return result

            last = result
            logger.warning(
                f"LLM调用失败: 第 {attempt}/{max_retries} 次, "
                f"error={result.get('error')!r}"
            )

            if attempt >= max_retries:
                break

            if cancel_check and callable(cancel_check):
                if cancel_check():
                    return {
                        "success": False,
                        "error": "任务已取消",
                        "result": None,
                        "text": "",
                    }

            wait_seconds = delay_min if delay_max <= delay_min else __import__("random").randint(delay_min, delay_max)
            logger.info(f"LLM调用等待重试: {wait_seconds} 秒")

            for _ in range(wait_seconds):
                if cancel_check and callable(cancel_check) and cancel_check():
                    return {
                        "success": False,
                        "error": "任务已取消",
                        "result": None,
                        "text": "",
                    }
                time.sleep(1)

        return last or {
            "success": False,
            "error": "未知错误",
            "result": None,
            "text": "",
        }
        

llm_service = LLMService()