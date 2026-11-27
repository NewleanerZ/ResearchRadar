"""Bounded Chat Completions requests and validated native tool messages."""

from dataclasses import dataclass
import time

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from .models import dump
from .ranking import request_completion, stream_completion


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


@dataclass(frozen=True)
class Tool:
    description: str
    arguments: type[Arguments]

    def definition(self, name):
        schema = self.arguments.model_json_schema()
        schema.pop("title", None)
        return {"type": "function", "function": {
            "name": name, "description": self.description, "parameters": schema}}


class ModelUnavailable(Exception):
    pass


class BudgetExceeded(ModelUnavailable):
    pass


def result(data=None, *, status="ok", code=None, retryable=False, references=()):
    return {"status": status, "data": data, "references": list(references),
            "error_code": code, "retryable": retryable}


def estimate_tokens(value) -> int:
    text = dump(value) if not isinstance(value, str) else value
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    ascii_chars = len(text) - non_ascii
    return max(1, (ascii_chars // 4) + non_ascii + 128)


class ModelBudget:
    def __init__(self, client, settings, seconds, calls, tokens, *, usage=None, save=None, trace=None):
        self.client = client
        self.settings = settings
        self.deadline = time.monotonic() + max(0, seconds)
        self.calls = calls
        self.tokens = tokens
        self.usage = usage if usage is not None else {}
        self.save = save or (lambda: None)
        self.trace = trace or (lambda *_args, **_kwargs: None)

    def complete(self, messages, *, tools=(), structured=False, max_tokens=1400,
                 thinking_level=None, stream=False, on_delta=None):
        if not self.settings.llm_model or not self.settings.llm_key.get_secret_value():
            raise ModelUnavailable("尚未配置对话模型")
        remaining = self.deadline - time.monotonic()
        estimated_input = estimate_tokens([messages, tools])
        reserved = self.usage.get("tokens", 0) + estimated_input + max_tokens
        reason = ("time" if remaining <= 0 else "calls" if self.usage.get("calls", 0) >= self.calls
                  else "tokens" if reserved > self.tokens else None)
        if reason:
            self.usage["stop_reason"] = reason
            self.save()
            self.trace("model", "budget", detail={"reason": reason, "calls": self.usage.get("calls", 0)})
            detail = {"time": "已达到本次处理的时间上限", "calls": f"已用完本次可用的 {self.calls} 次模型调用",
                      "tokens": "剩余 Token 不足以发起下一次模型请求"}
            raise BudgetExceeded(detail[reason])
        previous = self.usage.get("tokens", 0)
        self.usage.update(calls=self.usage.get("calls", 0) + 1, tokens=reserved,
                          unconfirmed_calls=self.usage.get("unconfirmed_calls", 0) + 1)
        self.save()
        started = time.monotonic()
        level = thinking_level or getattr(self.settings, "thinking_level", "off")
        self.trace("model", "running", detail={"model": self.settings.llm_model, "call": self.usage["calls"],
                                               "thinking": level})
        response = None
        for attempt in range(3):
            rem = self.deadline - time.monotonic()
            if rem <= 0.2:
                self.trace("model", "failed", detail={"error": "Timeout", "call": self.usage["calls"]},
                           elapsed_ms=int((time.monotonic() - started) * 1000))
                raise ModelUnavailable("模型暂时不可用")
            try:
                if stream and not structured:
                    response = stream_completion(
                        self.client, self.settings, messages, max_tokens, min(40, rem),
                        tools=tools, thinking_level=level, on_delta=on_delta,
                    )
                else:
                    response = request_completion(
                        self.client, self.settings, messages, max_tokens, min(40, rem),
                        tools=tools, structured=structured, thinking_level=level,
                    )
                break
            except (httpx.HTTPError, ValueError) as error:
                retryable = isinstance(error, (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError)) or (
                    isinstance(error, httpx.HTTPStatusError) and error.response.status_code in {429, 500, 502, 503, 504}
                )
                if retryable and attempt < 2 and (self.deadline - time.monotonic()) > 1.2:
                    wait = min(3.0, 0.5 * (2 ** attempt))
                    if isinstance(error, httpx.HTTPStatusError):
                        ra = error.response.headers.get("Retry-After", "")
                        if ra.isdigit():
                            wait = min(5.0, float(ra))
                    self.trace("model", "retrying", detail={"attempt": attempt + 1, "wait_s": wait})
                    time.sleep(wait)
                    continue
                detail = {"error": type(error).__name__, "call": self.usage["calls"]}
                if isinstance(error, httpx.HTTPStatusError):
                    detail["http_status"] = error.response.status_code
                self.trace("model", "failed", detail=detail, elapsed_ms=int((time.monotonic() - started) * 1000))
                raise ModelUnavailable("模型暂时不可用") from None
        usage = response.get("usage") or {}
        inputs, outputs = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if type(inputs) is int and type(outputs) is int and min(inputs, outputs) >= 0:
            self.usage["tokens"] = previous + inputs + outputs
            self.usage["unconfirmed_calls"] -= 1
            if self.settings.input_price is not None and self.settings.output_price is not None:
                self.usage["cost_usd"] = self.usage.get("cost_usd", 0) + (
                    inputs * self.settings.input_price + outputs * self.settings.output_price) / 1_000_000
            self.save()
        actual = {"call": self.usage["calls"], "model": self.settings.llm_model}
        if type(inputs) is int and type(outputs) is int:
            actual.update(input_tokens=inputs, output_tokens=outputs)
        self.trace("model", "completed", detail=actual, elapsed_ms=int((time.monotonic() - started) * 1000))
        return response


def model_message(response):
    try:
        message = response["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        content = message.get("content")
        reasoning = message.get("reasoning_content")
        if not isinstance(calls, list) or len(calls) > 8 or (content is not None and not isinstance(content, str)):
            raise ValueError
        if reasoning is not None and not isinstance(reasoning, str):
            raise ValueError
        if len({item["id"] for item in calls}) != len(calls) or any(
            not isinstance(item["id"], str) or not item["id"] or item["type"] != "function"
            or not isinstance(item["function"]["name"], str) or not isinstance(item["function"]["arguments"], str)
            for item in calls
        ):
            raise ValueError
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        raise ModelUnavailable("模型返回了无法读取的工具消息") from None
    out = {"role": "assistant", "content": content, **({"tool_calls": calls} if calls else {})}
    if reasoning:
        out["reasoning_content"] = reasoning
    return out


def execute_tool(call, tools, dispatch):
    name = call["function"]["name"]
    if name not in tools:
        return result("当前不能调用此工具", status="error", code="unknown_tool")
    try:
        args = tools[name].arguments.model_validate_json(call["function"]["arguments"])
    except ValidationError as error:
        tips = {"extra_forbidden": "未定义，请移除", "missing": "必填，请补充"}
        fields = [(".".join(map(str, item["loc"])) or "JSON") + " " + tips.get(item["type"], "格式或值无效")
                  for item in error.errors()]
        return result("请检查参数：" + "、".join(fields)[:280], status="error",
                      code="invalid_arguments", retryable=True)
    try:
        return dispatch(name, args)
    except ValueError as error:
        return result(str(error)[:250], status="error", code="invalid_target")
    except BudgetExceeded:
        return result("已达到预算", status="error", code="budget")
    except ModelUnavailable:
        return result("模型请求失败", status="error", code="model_unavailable")


def tool_message(call, outcome):
    return {"role": "tool", "tool_call_id": call["id"], "content": dump(outcome)}
