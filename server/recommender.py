"""Replaceable AI recommender adapter with strict output validation (task 5).

- The model only ever receives the current version's *available* garment
  IDs + color/style tags plus the scene and preference.  No photos, no body
  assets, no person data are sent.
- The model is constrained to return a structured result: a garment ID and a
  short reason, or no_match.  The server independently validates structure,
  ID membership, availability and the person/catalog binding; it never trusts
  the model to follow the prompt.
- mode=ai is used only for a real, successful, validated model call.
  Failures surface as errors (502/503); the server never silently substitutes
  a rule-based result and labels it ai.
- API keys live only in server-side env/local config, never in Android,
  source code, or logs.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Sequence

import httpx

SERVER_DIR = Path(__file__).resolve().parent
REPO_DIR = SERVER_DIR.parent


class RecommenderError(Exception):
    """Base class for recommender failures."""


class RecommenderTimeoutError(RecommenderError):
    """Provider did not answer within the configured timeout (503)."""


class RecommenderUpstreamError(RecommenderError):
    """Provider answered with an HTTP/transport error (503)."""


class InvalidModelOutputError(RecommenderError):
    """Output failed structure/ID validation (502)."""


class RateLimitError(RecommenderError):
    """Configured call/cost cap would be exceeded (503)."""


def build_prompt(candidates: Sequence[dict], scene: str, preference: str) -> list[dict]:
    """Build the system/user prompt for an OpenAI-compatible chat API."""
    garment_list = [
        {
            "id": candidate["asset_id"],
            "display_name": candidate.get("display_name"),
            "tags": candidate.get("tags", []),
        }
        for candidate in candidates
    ]
    system = (
        "你是「衣序 OOTD AI」的穿搭推荐助手。你的衣柜清单只包含当前版本可用衣物，"
        "必须且只能从清单中选择一件衣物。"
        "只输出一个严格 JSON 对象：{\"garment_id\": \"<ID>\" 或 null, \"reason\": \"一句简短理由\"}。"
        "若清单中没有合适的衣物，garment_id 必须为 null，reason 说明原因。"
        "不得输出 JSON 以外的内容，不得引入清单外的衣物，不得改变输出协议。"
    )
    user = (
        f"场合：{scene}\n"
        f"偏好：{preference}\n"
        f"可用衣物：{json.dumps(garment_list, ensure_ascii=False)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def validate_model_output(text: str, candidate_ids: set[str]) -> tuple[bool, dict]:
    """Validate the model's raw output against the allowed candidates.

    Returns (ok, result) where result is either
    {"status": "ok"/"no_match", "garment_id": ..., "reason": ...}
    or {"error": "<message>"}.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False, {"error": "模型输出不是合法 JSON"}
    if not isinstance(data, dict):
        return False, {"error": "模型输出必须是 JSON 对象"}
    garment_id = data.get("garment_id")
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return False, {"error": "模型输出缺少非空的 reason"}
    if garment_id is None:
        return True, {"status": "no_match", "garment_id": None, "reason": reason.strip()}
    if isinstance(garment_id, str) and garment_id in candidate_ids:
        return True, {"status": "ok", "garment_id": garment_id, "reason": reason.strip()}
    return False, {"error": f"模型返回未知或不可用的衣物 ID：{garment_id!r}"}


def check_daily_cap(conn, day: str, cap: int) -> None:
    """Count a call against the daily cap (persisted in SQLite)."""
    row = conn.execute(
        "SELECT calls FROM recommender_usage WHERE day=?", (day,)
    ).fetchone()
    calls = row["calls"] if row else 0
    if calls >= cap:
        raise RateLimitError(f"当日 AI 调用已达上限 {cap} 次")
    conn.execute(
        "INSERT INTO recommender_usage (day, calls) VALUES (?, 1) "
        "ON CONFLICT(day) DO UPDATE SET calls = calls + 1",
        (day,),
    )


def check_minute_cap(stamps: list[float], now: float, cap: int) -> None:
    """Count a call against the in-process per-minute cap (sliding window)."""
    cutoff = now - 60.0
    stamps[:] = [stamp for stamp in stamps if stamp > cutoff]
    if len(stamps) >= cap:
        raise RateLimitError("每分钟 AI 调用已达上限")
    stamps.append(now)


class AIModelRecommender:
    """OpenAI-compatible chat-completions adapter (e.g. DeepSeek API)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 20.0,
        client: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._client = client if client is not None else httpx.Client(timeout=timeout_seconds)

    def recommend(self, candidates: Sequence[dict], scene: str, preference: str):
        from dataclasses import dataclass

        @dataclass
        class ModelCall:
            status: str
            garment_id: str | None
            reason: str
            latency_seconds: float
            usage: dict | None
            raw: str

        started = time.perf_counter()
        payload = {
            "model": self.model,
            "messages": build_prompt(candidates, scene, preference),
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = self._client.post(
                f"{self.base_url}/chat/completions", headers=headers, json=payload
            )
        except httpx.TimeoutException as exc:
            raise RecommenderTimeoutError(f"模型调用超时（>{self.timeout_seconds}s）") from exc
        except httpx.HTTPError as exc:
            raise RecommenderUpstreamError(f"模型服务连接失败：{exc}") from exc
        latency = round(time.perf_counter() - started, 4)
        if response.status_code >= 400:
            raise RecommenderUpstreamError(
                f"模型服务返回 HTTP {response.status_code}"
            )
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            usage = body.get("usage")
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise InvalidModelOutputError("模型返回缺少 choices/message/content 字段") from exc

        candidate_ids = {c["asset_id"] for c in candidates}
        valid, result = validate_model_output(content, candidate_ids)
        if not valid:
            raise InvalidModelOutputError(result["error"])
        return ModelCall(
            status=result["status"],
            garment_id=result["garment_id"],
            reason=result["reason"],
            latency_seconds=latency,
            usage=usage,
            raw=content,
        )


def create_recommender_from_env() -> "AIModelRecommender":
    """Build the adapter from server-side environment configuration."""
    base_url = os.environ.get("OOTD_AI_BASE_URL", "").strip()
    api_key = os.environ.get("OOTD_AI_API_KEY", "").strip()
    model = os.environ.get("OOTD_AI_MODEL", "").strip()
    if not (base_url and api_key and model):
        raise RecommenderError(
            "AI 模式未配置：缺少 OOTD_AI_BASE_URL / OOTD_AI_API_KEY / OOTD_AI_MODEL"
        )
    timeout = float(os.environ.get("OOTD_AI_TIMEOUT_SECONDS", "20"))
    return AIModelRecommender(base_url=base_url, api_key=api_key, model=model, timeout_seconds=timeout)


def append_call_log(entry: dict) -> None:
    """Append one call record to the local JSONL log (no secrets inside)."""
    log_path = Path(os.environ.get("OOTD_CALL_LOG") or (REPO_DIR / "outputs" / "recommender-calls.jsonl"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")