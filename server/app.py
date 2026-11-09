"""Minimal FastAPI + SQLite backend for the OOTD AI LAN demo (task 4).

Four agreed endpoints (docs/architecture.md §6), a unified error body, an
optional bearer-token gate (token configured locally, never logged), and an
explicitly mocked recommender (mode=mock — no AI claims here).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import db
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from recommender import (
    InvalidModelOutputError,
    RateLimitError,
    RecommenderError,
    RecommenderTimeoutError,
    RecommenderUpstreamError,
    append_call_log,
    check_daily_cap,
    check_minute_cap,
    create_recommender_from_env,
)
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.status import HTTP_307_TEMPORARY_REDIRECT

SERVER_DIR = Path(__file__).resolve().parent
REPO_DIR = SERVER_DIR.parent


def _default_db_path() -> Path:
    return Path(os.environ.get("OOTD_DB_PATH") or str(REPO_DIR / "outputs" / "ootd.db"))


def _default_assets_root() -> Path:
    return Path(os.environ.get("OOTD_ASSETS_ROOT") or str(REPO_DIR / "outputs" / "server-assets"))


class RecommendationRequest(BaseModel):
    request_id: str = Field(min_length=1)
    person_id: str = Field(min_length=1)
    catalog_version: str = Field(min_length=1)
    scene: str = Field(min_length=1)
    preference: str = Field(min_length=1)


ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    404: "not_found",
    409: "version_conflict",
    422: "validation_error",
    500: "internal",
    502: "bad_gateway",
    503: "service_unavailable",
}


def _error_response(status: int, message: str, request_id: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": ERROR_CODES.get(status, "error"),
                "message": message,
                "request_id": request_id,
            }
        },
    )


def _request_id(request: Request) -> str | None:
    return request.headers.get("x-request-id")


# ---------------- explicit mock recommender (mode=mock) ----------------

WHITE_KEYWORDS = ("浅", "白", "亮")
DARK_KEYWORDS = ("深", "黑", "暗")
RED_KEYWORDS = ("红", "粉")
TAG_RULES = (("white", WHITE_KEYWORDS), ("dark", DARK_KEYWORDS), ("red", RED_KEYWORDS))


def mock_recommend(scene: str, preference: str, garments) -> tuple[str, str | None, str]:
    """Deterministic demo behaviour.  Candidates only come from available garments."""
    candidates = [row for row in garments if row["available"]]
    if not candidates:
        return "no_match", None, "当前版本没有可用衣物"

    def has_tag(row, tag: str) -> bool:
        return tag in json.loads(row["tags"])

    for tag, keywords in TAG_RULES:
        if any(keyword in preference for keyword in keywords):
            matched = [row for row in candidates if has_tag(row, tag)]
            if matched:
                return "ok", matched[0]["asset_id"], f"推荐 {matched[0]['display_name']}：符合场合与偏好（demo 模拟）"
            return "no_match", None, f"没有符合「{preference}」的可用衣物"

    chosen = candidates[0]
    return "ok", chosen["asset_id"], f"推荐 {chosen['display_name']}：符合场合与偏好（demo 模拟）"


# ---------------- app factory ----------------

def create_app(
    db_path=None,
    assets_root=None,
    token: str | None = None,
    recommender=None,
) -> FastAPI:
    """Create the FastAPI app.

    ``recommender``: optional pre-built AIModelRecommender used when
    OOTD_AI_MODE=ai (tests inject a fake server here; production builds it
    from environment variables instead).
    """
    db_location = Path(db_path or _default_db_path())
    storage_root = Path(assets_root or _default_assets_root())
    auth_token = token if token is not None else os.environ.get("OOTD_AUTH_TOKEN", "")
    minute_window: list[float] = []

    # make sure the schema exists before serving
    conn = db.connect(db_location)
    db.init_db(conn)
    conn.close()

    app = FastAPI(title="OOTD AI 服务端", version="0.1.0")

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        return _error_response(422, "请求字段错误", _request_id(request))

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == HTTP_307_TEMPORARY_REDIRECT:
            return exc  # keep redirects (e.g. docs) intact
        code = ERROR_CODES.get(exc.status_code, "error")
        return _error_response(exc.status_code, str(exc.detail) or code, _request_id(request))

    def require_auth(request: Request):
        # token configured locally; when unset the endpoint is open (dev mode)
        if not auth_token:
            return
        if request.headers.get("authorization") != f"Bearer {auth_token}":
            raise HTTPException(401, "未授权：缺少或错误的访问令牌")

    def open_db():
        conn = db.connect(db_location)
        try:
            yield conn
        finally:
            conn.close()

    @app.get("/v1/health")
    def health():
        return {"status": "ok"}

    @app.get("/v1/catalog", dependencies=[Depends(require_auth)])
    def catalog(person_id: str = Query(..., min_length=1)):
        conn = db.connect(db_location)
        try:
            version = db.get_current_version(conn, person_id)
            if version is None:
                raise HTTPException(404, f"人物不存在或未发布：{person_id}")
            assets = []
            for row in db.get_assets(conn, person_id, version["catalog_version"]):
                assets.append(
                    {
                        "asset_id": row["asset_id"],
                        "asset_type": row["asset_type"],
                        "display_name": row["display_name"],
                        "tags": json.loads(row["tags"]),
                        "available": bool(row["available"]),
                        "asset_url": (
                            f"/v1/assets/{person_id}/{version['catalog_version']}/{row['asset_id']}"
                        ),
                        "size_bytes": row["size_bytes"],
                        "sha256": row["sha256"],
                    }
                )
            return {
                "person_id": person_id,
                "catalog_version": version["catalog_version"],
                "pose_id": version["pose_id"],
                "assets": assets,
            }
        finally:
            conn.close()

    @app.get(
        "/v1/assets/{person_id}/{catalog_version}/{asset_id}",
        dependencies=[Depends(require_auth)],
    )
    def asset_file(person_id: str, catalog_version: str, asset_id: str):
        conn = db.connect(db_location)
        try:
            version = db.get_version(conn, person_id, catalog_version)
            if version is None:
                raise HTTPException(404, f"未找到资产：{person_id}/{catalog_version}/{asset_id}")
            row = db.get_asset(conn, person_id, catalog_version, asset_id)
            if row is None:
                raise HTTPException(404, f"未找到资产：{person_id}/{catalog_version}/{asset_id}")
            path = Path(version["storage_dir"]) / row["relative_file"]
            if not path.is_file():
                raise HTTPException(500, "已发布资产文件缺失，请重新导入")
            return FileResponse(path, media_type="model/gltf-binary")
        finally:
            conn.close()

    @app.post("/v1/recommendations", dependencies=[Depends(require_auth)])
    def recommendations(body: RecommendationRequest):
        conn = db.connect(db_location)
        try:
            version = db.get_version(conn, body.person_id, body.catalog_version)
            if version is None:
                raise HTTPException(404, f"人物不存在：{body.person_id}")
            current = db.get_current_version(conn, body.person_id)
            current_version = current["catalog_version"] if current else None
            if current_version != body.catalog_version:
                raise HTTPException(
                    409,
                    f"目录版本已过期：客户端 {body.catalog_version}，当前 {current_version}",
                )
            garments = [
                row
                for row in db.get_assets(conn, body.person_id, body.catalog_version)
                if row["asset_type"] == "garment"
            ]
            candidates = [
                {
                    "asset_id": row["asset_id"],
                    "display_name": row["display_name"],
                    "tags": json.loads(row["tags"]),
                }
                for row in garments
                if row["available"]
            ]

            mode = os.environ.get("OOTD_AI_MODE", "mock").lower()
            if mode == "mock":
                status, garment_id, reason = mock_recommend(
                    body.scene, body.preference, garments
                )
                response_mode = "mock"
            elif mode == "ai":
                try:
                    engine = recommender if recommender is not None else create_recommender_from_env()
                    daily_cap = int(os.environ.get("OOTD_AI_MAX_CALLS_PER_DAY", "1000"))
                    minute_cap = int(os.environ.get("OOTD_AI_MAX_CALLS_PER_MINUTE", "30"))
                    check_daily_cap(conn, time.strftime("%Y-%m-%d"), daily_cap)
                    check_minute_cap(minute_window, time.time(), minute_cap)
                    call = engine.recommend(candidates, body.scene, body.preference)
                except RecommenderTimeoutError as exc:
                    raise HTTPException(503, f"推荐超时：{exc}")
                except RecommenderUpstreamError as exc:
                    raise HTTPException(503, f"模型服务不可用：{exc}")
                except InvalidModelOutputError as exc:
                    raise HTTPException(502, f"模型输出校验失败：{exc}")
                except RateLimitError as exc:
                    raise HTTPException(503, str(exc))
                except RecommenderError as exc:
                    raise HTTPException(503, str(exc))
                status, garment_id, reason = call.status, call.garment_id, call.reason
                response_mode = "ai"
                append_call_log(
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "mode": "ai",
                        "status": status,
                        "garment_id": garment_id,
                        "reason": reason,
                        "request_id": body.request_id,
                        "latency_seconds": call.latency_seconds,
                        "usage": call.usage,
                    }
                )
            else:
                raise HTTPException(503, f"未知 OOTD_AI_MODE：{mode}")

            return {
                "request_id": body.request_id,
                "person_id": body.person_id,
                "catalog_version": body.catalog_version,
                "status": status,
                "garment_id": garment_id,
                "reason": reason,
                "mode": response_mode,
            }
        finally:
            conn.close()

    return app


app = create_app()