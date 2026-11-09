"""飞书双向桥：长连接收训练员私信 → 回调；主动发/改私信。

只用 lark_oapi 的长连接（WebSocket）模式，不需要公网回调地址。

约定：
- 只处理 ``im.message.receive_v1`` 里 **p2p** 且发送者 open_id 等于训练员的文本消息；
- 入向有两种模式：
  * ``on_chat``（聊天模式）：收到文本直接交给它（带 message_id/open_id），
    确认语与进度由 ChatService 负责，这里不再发确认；
  * ``on_text``（任务汇报模式）：交给 worker POST /notice(urgent)，
    成功后再回一句确认；失败则回一句“转发失败”，不让训练员干等；
- 出向：``send_text`` / ``send_text_message`` 用应用机器人身份发私信，
  ``update_text`` 编辑已发消息（流式进度用）；
- 断线：lark 客户端自身 auto_reconnect，外层再兜一层重启循环；
- 日志：用调用方传进来的 logger（worker 自己的日志文件）。

lark_oapi 在 ``_ensure_clients`` 里懒加载，模块本身可被无 lark 环境导入。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

# 训练员的 open_id。注意：open_id 是“应用维度”的，同一个用户在不同飞书应用下
# 值不同；因此优先用凭据文件里的 open_id / --feishu-open-id，这里只是兜底。
DEFAULT_TRAINER_OPEN_ID = "ou_e5c3f85b727b639d84caa47f74d24311"
DEFAULT_CONFIRM_TEXT = "收到，已转给溪流"
CONFIRM_FAILED_TEXT = "收到，但转给溪流失败（Router 没接上），请稍后再发一次"

DOMAIN_URLS = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}

log = logging.getLogger("mowu-worker.feishu")


class FeishuError(Exception):
    """飞书配置/凭据错误。"""


def _expand(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value or ""))


def _get(obj: Any, *path: str, default: Any = None) -> Any:
    """沿属性/字典路径取值，任意一层缺失返回 default。"""
    current = obj
    for key in path:
        if current is None:
            return default
        if isinstance(current, dict):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
    return default if current is None else current


@dataclass
class FeishuCreds:
    app_id: str
    app_secret: str
    domain: str = "feishu"
    bot_name: str = ""
    open_id: str = ""

    @property
    def domain_url(self) -> str:
        value = (self.domain or "feishu").strip()
        if value.startswith("http://") or value.startswith("https://"):
            return value.rstrip("/")
        return DOMAIN_URLS.get(value.lower(), DOMAIN_URLS["feishu"])


def load_creds(path: str) -> FeishuCreds:
    """读 app_id/app_secret/domain/bot_name/open_id（后两项可缺）。"""
    target = Path(_expand(path))
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FeishuError("飞书凭据文件不存在：%s" % target)
    except OSError as exc:
        raise FeishuError("飞书凭据文件读取失败：%s（%s）" % (target, exc))
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise FeishuError("飞书凭据不是合法 JSON：%s（%s）" % (target, exc))
    if not isinstance(data, dict):
        raise FeishuError("飞书凭据必须是 JSON 对象：%s" % target)
    app_id = str(data.get("app_id") or "").strip()
    app_secret = str(data.get("app_secret") or "").strip()
    if not app_id or not app_secret:
        raise FeishuError("飞书凭据缺 app_id/app_secret：%s" % target)
    return FeishuCreds(
        app_id=app_id,
        app_secret=app_secret,
        domain=str(data.get("domain") or "feishu"),
        bot_name=str(data.get("bot_name") or data.get("name") or ""),
        open_id=str(data.get("open_id") or "").strip(),
    )


def strip_mentions(text: str) -> str:
    """去掉飞书文本里的 @ 占位符（p2p 一般没有，群聊转发时可能有）。"""
    out = []
    index = 0
    while index < len(text):
        if text.startswith("@_user_", index):
            cursor = index + len("@_user_")
            while cursor < len(text) and text[cursor].isdigit():
                cursor += 1
            index = cursor
            continue
        out.append(text[index])
        index += 1
    return "".join(out).strip()


class FeishuBridge:
    """一个 bot 的长连接 + 私信发送器。

    测试时通过 ``api_client`` / ``ws_client`` 注入假对象，不会真连飞书。
    """

    def __init__(
        self,
        creds: FeishuCreds,
        open_id: str,
        on_text: Optional[Callable[[str], bool]] = None,
        confirm_text: str = DEFAULT_CONFIRM_TEXT,
        logger: Optional[logging.Logger] = None,
        api_client: Any = None,
        ws_client: Any = None,
        ws_factory: Optional[Callable[[], Any]] = None,
        api_factory: Optional[Callable[[], Any]] = None,
        restart_delay: float = 5.0,
        max_restart_delay: float = 60.0,
        on_chat: Optional[Callable[[str, str, str], Any]] = None,
    ) -> None:
        self.creds = creds
        self.open_id = (open_id or "").strip()
        self.on_text = on_text
        self.on_chat = on_chat
        self.confirm_text = confirm_text or DEFAULT_CONFIRM_TEXT
        self.log = logger or log
        self._api = api_client
        self._ws = ws_client
        self._ws_factory = ws_factory
        self._api_factory = api_factory
        self._restart_delay = max(0.05, float(restart_delay))
        self._max_restart_delay = max(self._restart_delay,
                                      float(max_restart_delay))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.started = threading.Event()
        self.connecting = 0
        self.handled = 0
        self.sent = 0
        self.updated = 0

    # -- 客户端 ---------------------------------------------------------- #

    def _ensure_api(self) -> Any:
        with self._lock:
            if self._api is None:
                if self._api_factory is not None:
                    self._api = self._api_factory()
                else:
                    import lark_oapi as lark

                    self._api = (
                        lark.Client.builder()
                        .app_id(self.creds.app_id)
                        .app_secret(self.creds.app_secret)
                        .domain(self.creds.domain_url)
                        .timeout(10.0)
                        .build()
                    )
            return self._api

    def _ensure_ws(self) -> Any:
        with self._lock:
            if self._ws is None:
                if self._ws_factory is not None:
                    self._ws = self._ws_factory()
                else:
                    import lark_oapi as lark

                    handler = (
                        lark.EventDispatcherHandler.builder("", "")
                        .register_p2_im_message_receive_v1(self.handle_event)
                        .build()
                    )
                    self._ws = lark.ws.Client(
                        self.creds.app_id,
                        self.creds.app_secret,
                        domain=self.creds.domain_url,
                        event_handler=handler,
                        log_level=lark.LogLevel.INFO,
                        auto_reconnect=True,
                    )
            return self._ws

    # -- 出向 ------------------------------------------------------------ #

    def _build_message_request(self, text: str) -> Any:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        body = (
            CreateMessageRequestBody.builder()
            .receive_id(self.open_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        return (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(body)
            .build()
        )

    def _build_update_request(self, message_id: str, text: str) -> Any:
        from lark_oapi.api.im.v1 import UpdateMessageRequest, UpdateMessageRequestBody

        body = (
            UpdateMessageRequestBody.builder()
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        return (
            UpdateMessageRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )

    @staticmethod
    def _response_ok(response: Any) -> bool:
        try:
            return bool(response.success())
        except Exception:  # noqa: BLE001 - 假对象/新版 SDK 可能没有 success()
            return getattr(response, "code", 0) == 0

    @staticmethod
    def _response_message_id(response: Any) -> str:
        """从 create 响应里取 message_id；取不到返回空串（不影响发送成功判定）。"""
        data = getattr(response, "data", None)
        message_id = _get(data, "message_id", default="") if data is not None else ""
        return str(message_id or "")

    def send_text_message(self, text: str) -> str:
        """给训练员发一条私信，成功返回 message_id；失败只记日志，不抛异常。"""
        text = (text or "").strip()
        if not text:
            return ""
        if not self.open_id:
            self.log.warning("飞书发送跳过：没有训练员 open_id")
            return ""
        try:
            request = self._build_message_request(text)
            response = self._ensure_api().im.v1.message.create(request)
            if not self._response_ok(response):
                self.log.warning("飞书发送失败 code=%s msg=%s",
                                 getattr(response, "code", "?"),
                                 getattr(response, "msg", ""))
                return ""
            self.sent += 1
            message_id = self._response_message_id(response)
            self.log.info("飞书已发送 → %s：%s", self.open_id, text[:120])
            return message_id
        except Exception as exc:  # noqa: BLE001 - 网络/凭据问题不能拖垮 worker
            self.log.warning("飞书发送异常：%s", exc)
            return ""

    def send_text(self, text: str) -> bool:
        """发一条私信；返回是否成功（兼容旧调用方）。"""
        text = (text or "").strip()
        if not text:
            return False
        if not self.open_id:
            self.log.warning("飞书发送跳过：没有训练员 open_id")
            return False
        try:
            request = self._build_message_request(text)
            response = self._ensure_api().im.v1.message.create(request)
            if not self._response_ok(response):
                self.log.warning("飞书发送失败 code=%s msg=%s",
                                 getattr(response, "code", "?"),
                                 getattr(response, "msg", ""))
                return False
            self.sent += 1
            self.log.info("飞书已发送 → %s：%s", self.open_id, text[:120])
            return True
        except Exception as exc:  # noqa: BLE001 - 网络/凭据问题不能拖垮 worker
            self.log.warning("飞书发送异常：%s", exc)
            return False

    def update_text(self, message_id: str, text: str) -> bool:
        """编辑一条已发文本消息（流式进度用）；失败只记日志。"""
        message_id = (message_id or "").strip()
        text = (text or "").strip()
        if not message_id or not text:
            return False
        try:
            request = self._build_update_request(message_id, text)
            response = self._ensure_api().im.v1.message.update(request)
            if not self._response_ok(response):
                self.log.warning("飞书编辑失败 id=%s code=%s msg=%s",
                                 message_id, getattr(response, "code", "?"),
                                 getattr(response, "msg", ""))
                return False
            self.updated += 1
            self.log.debug("飞书已编辑 id=%s：%s", message_id, text[:80])
            return True
        except Exception as exc:  # noqa: BLE001
            self.log.warning("飞书编辑异常 id=%s：%s", message_id, exc)
            return False

    # -- 入向 ------------------------------------------------------------ #

    def handle_event(self, event: Any) -> None:
        """lark 事件回调入口（也供自测直接调用）。永不抛异常。"""
        try:
            self._handle_event(event)
        except Exception:  # noqa: BLE001
            self.log.exception("飞书事件处理失败")

    def _handle_event(self, event: Any) -> None:
        message = _get(event, "event", "message")
        sender = _get(event, "event", "sender", "sender_id", "open_id", default="")
        chat_type = str(_get(message, "chat_type", default="") or "")
        message_type = str(_get(message, "message_type", default="text") or "text")

        if chat_type != "p2p":
            self.log.debug("忽略非私信消息 chat_type=%s", chat_type or "?")
            return
        if not self.open_id or str(sender) != self.open_id:
            self.log.warning("忽略非训练员私信 sender=%s（期望 %s）",
                             sender or "?", self.open_id or "(未配置)")
            return
        if message_type != "text":
            self.log.info("忽略非文本私信 type=%s", message_type)
            return

        content = _get(message, "content", default="")
        text = ""
        try:
            parsed = json.loads(content) if isinstance(content, str) else content
            if isinstance(parsed, dict):
                text = str(parsed.get("text") or "")
        except (ValueError, TypeError):
            text = str(content or "")
        text = strip_mentions(text)
        if not text:
            self.log.info("忽略空文本私信")
            return

        self.handled += 1
        message_id = str(_get(message, "message_id", default="") or "")
        self.log.info("收到训练员飞书私信：%s", text[:200])
        if self.on_chat is not None:
            try:
                self.on_chat(text, message_id, str(sender))
            except Exception:  # noqa: BLE001
                self.log.exception("飞书聊天回调失败")
            return
        if self.on_text is None:
            self.log.warning("飞书入向没有回调（on_text/on_chat 均为空），忽略")
            return
        forwarded = False
        try:
            forwarded = bool(self.on_text(text))
        except Exception:  # noqa: BLE001
            self.log.exception("飞书入向转发失败")
        if forwarded:
            self.send_text(self.confirm_text)
        else:
            self.send_text(CONFIRM_FAILED_TEXT)

    # -- 生命周期 -------------------------------------------------------- #

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="feishu-ws",
                                        daemon=True)
        self._thread.start()
        self.log.info("飞书长连接线程已启动 bot=%s domain=%s",
                      self.creds.bot_name or self.creds.app_id,
                      self.creds.domain_url)

    def _run(self) -> None:
        delay = self._restart_delay
        while not self._stop.is_set():
            try:
                ws = self._ensure_ws()
                self.connecting += 1
                self.started.set()
                ws.start()
                if self._stop.is_set():
                    return
                self.log.warning("飞书长连接返回但未停止，准备重连")
                delay = self._restart_delay
            except Exception as exc:  # noqa: BLE001 - 长连接必须自愈
                if self._stop.is_set():
                    return
                self.log.warning("飞书长连接异常：%s（%ss 后重连）",
                                 exc, delay)
            if self._stop.wait(delay):
                return
            delay = min(delay * 2, self._max_restart_delay)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        ws = self._ws
        for name in ("stop", "close"):
            func = getattr(ws, name, None)
            if callable(func):
                try:
                    func()
                except Exception:  # noqa: BLE001
                    pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def wait_connected(self, timeout: float = 5.0) -> bool:
        return self.started.wait(timeout)
