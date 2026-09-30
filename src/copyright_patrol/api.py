"""基于标准库的 HTTP 接口。

请求/响应均为 JSON，时间字段使用 ISO-8601。业务错误统一返回
``{"error": {"code", "message", ...}}`` 结构。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .errors import DomainError, PublicationBlocked
from .repository import Repository
from .service import CopyrightService


def _encode(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"不可序列化的类型：{type(value)!r}")


def _dt(data: dict, key: str, required: bool = False) -> datetime | None:
    raw = data.get(key)
    if raw is None:
        if required:
            raise ValueError(f"缺少时间字段：{key}")
        return None
    return datetime.fromisoformat(raw)


def create_handler(service: CopyrightService, on_mutation: Callable[[], None] | None = None) -> type[BaseHTTPRequestHandler]:
    service_lock = threading.RLock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "CopyrightPatrol/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # 安静的测试日志
            return

        # ---- 基础收发 ----
        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"请求体不是合法 JSON：{exc}") from exc
            if not isinstance(data, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return data

        def _send(self, status: int, payload: Any) -> None:
            # 立即序列化为字节：成功响应在服务锁内调用，避免遍历时数据被并发修改
            self._response = (
                status,
                json.dumps(payload, ensure_ascii=False, default=_encode).encode("utf-8"),
            )

        def _flush(self) -> None:
            status, body = self._response
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _ok(self, payload: Any, status: int = 200) -> None:
            self._send(status, {"data": payload})

        # ---- 路由 ----
        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            self._response: tuple[int, Any] | None = None
            try:
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                data = self._read_json() if method == "POST" else {}
                # 所有请求共用一把锁：写变更、快照序列化、读遍历互不交叉
                with service_lock:
                    self._route(method, path, data)
                    if method == "POST" and on_mutation is not None:
                        on_mutation()
            except PublicationBlocked as exc:
                self._send(exc.status, {
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                        "violations": exc.violations,
                    }
                })
            except DomainError as exc:
                self._send(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
            except (ValueError, TypeError) as exc:
                self._send(400, {"error": {"code": "bad_request", "message": str(exc)}})
            except OSError as exc:
                self._send(500, {"error": {"code": "persist_failed", "message": str(exc)}})
            finally:
                if self._response is not None:
                    self._flush()

        def _route(self, method: str, path: str, data: dict) -> None:
            handler = ROUTES.get((method, path))
            if handler is None:
                static, dynamic = _match(method, path)
                if dynamic is not None:
                    dynamic(self, service, data, static)
                else:
                    self._send(404, {"error": {"code": "not_found", "message": f"无此接口：{method} {path}"}})
            else:
                handler(self, service, data)

    return Handler


# 简单的参数化路由：以 {name} 标记路径段
_Route = Callable[..., None]
STATIC_ROUTES: dict[tuple[str, str], _Route] = {}
DYNAMIC_ROUTES: list[tuple[str, list[str], _Route]] = []


def route(method: str, pattern: str) -> Callable[[_Route], _Route]:
    parts = [p for p in pattern.strip("/").split("/") if p]

    def wrapper(fn: _Route) -> _Route:
        if any(p.startswith("{") and p.endswith("}") for p in parts):
            DYNAMIC_ROUTES.append((method, parts, fn))
        else:
            STATIC_ROUTES[(method, "/" + "/".join(parts))] = fn
        return fn

    return wrapper


ROUTES = STATIC_ROUTES


def _match(method: str, path: str):
    segments = [p for p in path.strip("/").split("/") if p]
    for m, parts, fn in DYNAMIC_ROUTES:
        if m != method or len(parts) != len(segments):
            continue
        params: dict[str, str] = {}
        for part, segment in zip(parts, segments):
            if part.startswith("{") and part.endswith("}"):
                params[part[1:-1]] = segment
            elif part != segment:
                break
        else:
            return params, fn
    return None, None


# ============ 接口实现 ============

@route("GET", "/health")
def _health(h, svc: CopyrightService, data: dict) -> None:
    h._ok({"status": "ok"})


@route("POST", "/holders")
def _create_holder(h, svc, data):
    h._ok(svc.register_holder(data["holder_id"], data["name"], data.get("contact", "")), 201)


@route("POST", "/materials")
def _create_material(h, svc, data):
    h._ok(
        svc.register_material(
            data["material_id"], data["title"], data["kind"],
            data["holder_id"], data.get("alternative_ids"),
            data.get("license_free", False),
        ),
        201,
    )


@route("POST", "/materials/{material_id}/alternatives")
def _link_alternative(h, svc, data, params):
    h._ok(svc.link_alternative(params["material_id"], data["alternative_id"]))


@route("POST", "/licenses")
def _create_license(h, svc, data):
    h._ok(
        svc.register_license(
            data["license_id"], data["material_id"], data["scope"],
            _dt(data, "valid_from", True), _dt(data, "valid_until", True),
            data.get("territory"), data.get("note", ""),
        ),
        201,
    )


@route("POST", "/licenses/{license_id}/renew")
def _renew(h, svc, data, params):
    h._ok(svc.renew_license(
        params["license_id"],
        _dt(data, "valid_from", True), _dt(data, "valid_until", True),
        data.get("scope"), data.get("territory"), data.get("note", ""),
    ))


@route("POST", "/licenses/{license_id}/revoke")
def _revoke(h, svc, data, params):
    h._ok(svc.revoke_license(params["license_id"], data.get("reason", ""), _dt(data, "effective_at")))


@route("POST", "/licenses/{license_id}/suspend")
def _suspend(h, svc, data, params):
    h._ok(svc.suspend_license(
        params["license_id"],
        _dt(data, "suspend_from", True), _dt(data, "suspend_until", True),
        data.get("reason", ""),
    ))


@route("POST", "/licenses/{license_id}/resume")
def _resume(h, svc, data, params):
    h._ok(svc.resume_license(params["license_id"], data.get("reason", ""), _dt(data, "effective_at")))


@route("GET", "/licenses/{license_id}")
def _get_license(h, svc, data, params):
    h._ok(svc.get_license(params["license_id"]))


@route("GET", "/licenses/{license_id}/versions/{seq}/impact")
def _license_impact(h, svc, data, params):
    h._ok(svc.impact_of_license_version(params["license_id"], int(params["seq"])))


@route("POST", "/plans")
def _create_plan(h, svc, data):
    h._ok(svc.register_plan(data["plan_id"], data["title"], data["references"]), 201)


@route("GET", "/plans/{plan_id}")
def _get_plan(h, svc, data, params):
    h._ok(svc.get_plan(params["plan_id"]))


@route("POST", "/replacements")
def _replace(h, svc, data):
    h._ok(
        svc.replace_plan_materials(data["mappings"], data["reason"], data.get("command_id")),
        201,
    )


@route("GET", "/replacements/{command_id}/impact")
def _replace_impact(h, svc, data, params):
    h._ok(svc.impact_of_replace_command(params["command_id"]))


@route("POST", "/sessions")
def _create_session(h, svc, data):
    h._ok(svc.schedule_session(
        data["session_id"], data["title"], data["plan_id"],
        _dt(data, "start", True), _dt(data, "end", True),
        data.get("territory"),
    ), 201)


@route("GET", "/sessions/{session_id}")
def _get_session(h, svc, data, params):
    h._ok(svc.get_session(params["session_id"]))


@route("POST", "/sessions/{session_id}/publish")
def _publish(h, svc, data, params):
    h._ok(svc.publish_session(params["session_id"]))


@route("POST", "/sessions/{session_id}/complete")
def _complete(h, svc, data, params):
    h._ok(svc.complete_session(params["session_id"]))


@route("POST", "/sessions/{session_id}/cancel")
def _cancel(h, svc, data, params):
    h._ok(svc.cancel_session(params["session_id"], data.get("reason", "")))


@route("POST", "/patrols")
def _patrol(h, svc, data):
    result = svc.run_patrol(data["batch_id"], _dt(data, "as_of"), _dt(data, "horizon"))
    h._ok(result, 200 if result.get("idempotent_replay") else 201)


@route("GET", "/patrols/{batch_id}")
def _get_patrol(h, svc, data, params):
    from .errors import NotFoundError

    batch = svc.repo.patrol_batches.get(params["batch_id"])
    if batch is None:
        raise NotFoundError(f"巡检批次不存在：{params['batch_id']}")
    h._ok(batch)


@route("GET", "/cases")
def _list_cases(h, svc, data):
    h._ok(svc.list_open_cases())


@route("GET", "/cases/{case_id}")
def _get_case(h, svc, data, params):
    h._ok(svc.get_case(params["case_id"]))


@route("GET", "/materials/{material_id}/impact")
def _material_impact(h, svc, data, params):
    h._ok(svc.impact_of_material(params["material_id"]))


@route("POST", "/chains/verify")
def _verify(h, svc, data):
    h._ok(svc.verify_chains())


def build_server(
    host: str,
    port: int,
    service: CopyrightService,
    on_mutation: Callable[[], None] | None = None,
) -> "PatrolHTTPServer":
    return PatrolHTTPServer((host, port), create_handler(service, on_mutation))


class PatrolHTTPServer(ThreadingHTTPServer):
    """追踪在途请求，关停时可等待排空，避免落盘与目录清理竞态。"""

    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._inflight = 0
        self._condition = threading.Condition()

    def process_request(self, request: Any, client_address: Any) -> None:
        with self._condition:
            self._inflight += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._condition:
                self._inflight -= 1
                if self._inflight == 0:
                    self._condition.notify_all()
            raise

    def close_request(self, request: Any) -> None:
        super().close_request(request)
        with self._condition:
            self._inflight -= 1
            if self._inflight == 0:
                self._condition.notify_all()

    def drain(self, timeout: float = 5.0) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: self._inflight == 0, timeout)


def create_app_service(path: str | None = None) -> CopyrightService:
    """从快照文件加载服务；文件不存在时使用空库（首次启动）。"""
    from pathlib import Path

    if path and Path(path).exists():
        return CopyrightService(Repository.load(path))
    return CopyrightService()
