"""
微信兼容层。

当前默认优先走 OneBot v11 本地网关，避免再直接依赖 wxauto。
如果确实要回退旧版桌面自动化，可设置:

    KOURICHAT_WECHAT_BACKEND=wxauto
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import httpx
import requests
from httpx_ws import aconnect_ws

logger = logging.getLogger("main")

_BACKEND_ENV = "KOURICHAT_WECHAT_BACKEND"
_ONEBOT_HTTP_ENV = "KOURICHAT_ONEBOT_HTTP"
_ONEBOT_WS_ENV = "KOURICHAT_ONEBOT_WS"
_ONEBOT_TOKEN_ENV = "KOURICHAT_ONEBOT_TOKEN"

_DEFAULT_ONEBOT_HTTP = "http://127.0.0.1:5700"
_DEFAULT_ONEBOT_WS = "ws://127.0.0.1:5700/"
_DEFAULT_ONEBOT_TOKEN = "change-me"

_ONEBOT_SESSION: Optional["_OneBotSession"] = None
_ONEBOT_SESSION_LOCK = threading.Lock()


class _BackendProxy:
    """薄代理：保持原有属性/方法访问方式不变。"""

    def __init__(self, target: Any):
        self._target = target

    def __getattr__(self, item: str) -> Any:
        return getattr(self._target, item)


class _LegacyWxautoBackend:
    """旧版后端：真实 wxauto。"""

    def __init__(self) -> None:
        from wxauto import WeChat as LegacyWeChat
        from wxauto.elements import ChatWnd as LegacyChatWnd

        self._wechat_cls = LegacyWeChat
        self._chat_wnd_cls = LegacyChatWnd

    def create_wechat(self, *args: Any, **kwargs: Any) -> Any:
        return self._wechat_cls(*args, **kwargs)

    def create_chat_wnd(self, *args: Any, **kwargs: Any) -> Any:
        return self._chat_wnd_cls(*args, **kwargs)


def _with_access_token(url: str, token: str) -> str:
    if not token:
        return url
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("access_token", token)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _normalize_file_uri(filepath: str) -> str:
    if filepath.startswith(("file://", "http://", "https://")):
        return filepath
    return Path(filepath).resolve().as_uri()


def _segment_content_to_text(message: Any) -> str:
    if isinstance(message, str):
        return message

    parts: list[str] = []
    if not isinstance(message, list):
        return ""

    for segment in message:
        if not isinstance(segment, dict):
            continue
        seg_type = segment.get("type")
        data = segment.get("data", {}) or {}
        if seg_type == "text":
            parts.append(str(data.get("text", "")))
        elif seg_type in {"image", "file", "video", "record"}:
            parts.append(str(data.get("file", "")))
        else:
            text = data.get("text")
            if text:
                parts.append(str(text))

    return " ".join(part for part in parts if part).strip()


class _NameProxy:
    def __init__(self, getter):
        self._getter = getter

    @property
    def Name(self) -> str:
        return self._getter()


class _NullUiControl:
    def Exists(self, *_args: Any, **_kwargs: Any) -> bool:
        return False

    def ButtonControl(self, *_args: Any, **_kwargs: Any) -> "_NullUiControl":
        return self

    def Click(self, *_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError("OneBot 后端不支持桌面语音通话按钮操作")


class _OneBotChatWnd:
    def __init__(self, who: str, language: str = "cn"):
        self.who = who
        self.language = language
        self.UiaAPI = _NullUiControl()

    def _show(self) -> None:
        return


@dataclass(frozen=True)
class _CompatChat:
    who: str


class _CompatMessage:
    def __init__(self, event: dict[str, Any]):
        self.id = str(event.get("message_id", ""))
        self.type = "friend" if event.get("message_type") == "private" else str(event.get("message_type", ""))
        self.sender = str((event.get("sender") or {}).get("nickname") or event.get("user_id") or "")
        self.user_id = str(event.get("user_id") or "")
        self.content = _segment_content_to_text(event.get("message"))
        self.text = self.content


class _OneBotSession:
    def __init__(self) -> None:
        self.http_url = os.getenv(_ONEBOT_HTTP_ENV, _DEFAULT_ONEBOT_HTTP).rstrip("/")
        self.ws_url = os.getenv(_ONEBOT_WS_ENV, _DEFAULT_ONEBOT_WS)
        self.token = os.getenv(_ONEBOT_TOKEN_ENV, _DEFAULT_ONEBOT_TOKEN)
        self.language = "cn"
        self.current_chat: Optional[str] = None
        self.listen_targets: set[str] = set()
        self.listen_all = True
        self.incoming_events: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.online = False
        self.login_info = {"user_id": "onebot", "nickname": "KouriChat"}
        self.chat_box = _NullUiControl()
        self._worker_started = False
        self._worker_lock = threading.Lock()

        self.refresh_login_info()
        self.start_worker()

    @property
    def service_name(self) -> str:
        nickname = str(self.login_info.get("nickname") or "").strip()
        return nickname or str(self.login_info.get("user_id") or "KouriChat")

    def _request_headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    def _post_action(self, action: str, payload: Optional[dict[str, Any]] = None, timeout: float = 10.0) -> dict[str, Any]:
        url = f"{self.http_url}/{action}"
        response = requests.post(
            url,
            params={"access_token": self.token} if self.token else None,
            headers=self._request_headers(),
            json=payload or {},
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    def refresh_login_info(self) -> None:
        try:
            result = self._post_action("get_login_info", timeout=5.0)
            data = result.get("data") or {}
            self.login_info = {
                "user_id": str(data.get("user_id") or "onebot"),
                "nickname": str(data.get("nickname") or data.get("user_id") or "KouriChat"),
            }
            self.online = True
            logger.info(f"OneBot 微信兼容层已连接，当前账号: {self.login_info['nickname']}")
        except Exception as exc:
            self.online = False
            logger.warning(f"OneBot 登录信息获取失败，将等待网关就绪: {exc}")

    def start_worker(self) -> None:
        with self._worker_lock:
            if self._worker_started:
                return
            thread = threading.Thread(target=self._run_worker, name="OneBotWechatCompat", daemon=True)
            thread.start()
            self._worker_started = True

    def _run_worker(self) -> None:
        asyncio.run(self._ws_loop())

    async def _ws_loop(self) -> None:
        ws_url = _with_access_token(self.ws_url, self.token)
        while True:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with aconnect_ws(ws_url, client) as ws:
                        self.online = True
                        logger.info(f"OneBot 事件流已连接: {ws_url}")
                        while True:
                            event = await ws.receive_json()
                            if not isinstance(event, dict):
                                continue
                            post_type = event.get("post_type")
                            if post_type == "meta_event":
                                if event.get("meta_event_type") == "lifecycle":
                                    self.refresh_login_info()
                                continue
                            if post_type == "message":
                                self.incoming_events.put(event)
            except Exception as exc:
                self.online = False
                logger.warning(f"OneBot 事件流断开，2秒后重连: {exc}")
                await asyncio.sleep(2)

    def get_session_list(self) -> list[str]:
        if self.current_chat:
            return [self.current_chat]
        return [str(self.login_info.get("user_id") or "onebot")]

    def chat_with(self, who: str) -> bool:
        self.current_chat = str(who)
        return True

    def add_listen_chat(self, who: Optional[str]) -> bool:
        if not who or str(who).strip() in {"*", "__all__", "ALL"}:
            self.listen_all = True
            return True
        self.listen_targets.add(str(who))
        return True

    def should_accept(self, who: str) -> bool:
        if self.listen_all or not self.listen_targets:
            return True
        return who in self.listen_targets

    def drain_events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        while True:
            try:
                events.append(self.incoming_events.get_nowait())
            except queue.Empty:
                break
        return events

    def send_text(self, msg: str, who: str) -> dict[str, Any]:
        return self._post_action(
            "send_private_msg",
            {"user_id": str(who), "message": str(msg), "auto_escape": True},
        )

    def send_file(self, filepath: str, who: str) -> dict[str, Any]:
        file_uri = _normalize_file_uri(filepath)
        suffix = Path(filepath).suffix.lower()
        seg_type = "image" if suffix in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"} else "file"
        return self._post_action(
            "send_private_msg",
            {"user_id": str(who), "message": [{"type": seg_type, "data": {"file": file_uri}}]},
        )


def _get_onebot_session() -> _OneBotSession:
    global _ONEBOT_SESSION
    with _ONEBOT_SESSION_LOCK:
        if _ONEBOT_SESSION is None:
            _ONEBOT_SESSION = _OneBotSession()
        return _ONEBOT_SESSION


class _OneBotWeChatClient:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self._session = _get_onebot_session()
        self.language = self._session.language
        self.A_MyIcon = _NameProxy(lambda: self._session.service_name)
        self.ChatBox = self._session.chat_box

    def _show(self) -> None:
        return

    def GetSessionList(self) -> list[str]:
        return self._session.get_session_list()

    def ChatWith(self, who: str, timeout: float = 2) -> bool:
        del timeout
        return self._session.chat_with(str(who))

    def AddListenChat(
        self,
        who: Optional[str],
        savepic: bool = False,
        savefile: bool = False,
        savevoice: bool = False,
    ) -> bool:
        del savepic, savefile, savevoice
        return self._session.add_listen_chat(who)

    def GetListenMessage(self, who: Optional[str] = None) -> dict[_CompatChat, list[_CompatMessage]]:
        messages: dict[_CompatChat, list[_CompatMessage]] = {}
        for event in self._session.drain_events():
            if event.get("post_type") != "message":
                continue
            if event.get("message_type") != "private":
                continue

            user_id = str(event.get("user_id") or "")
            if who and str(who) != user_id:
                continue
            if not self._session.should_accept(user_id):
                continue

            chat = _CompatChat(who=user_id)
            messages.setdefault(chat, []).append(_CompatMessage(event))

        return messages

    def SendMsg(self, msg: str, who: Optional[str] = None, clear: bool = True, at: Any = None) -> Any:
        del clear, at
        target = str(who or self._session.current_chat or "")
        if not target:
            raise ValueError("未指定消息接收对象")
        return self._session.send_text(msg, target)

    def SendFiles(self, filepath: str, who: Optional[str] = None) -> Any:
        target = str(who or self._session.current_chat or "")
        if not target:
            raise ValueError("未指定文件接收对象")
        return self._session.send_file(filepath, target)


class _OneBotBackend:
    def create_wechat(self, *args: Any, **kwargs: Any) -> Any:
        return _OneBotWeChatClient(*args, **kwargs)

    def create_chat_wnd(self, *args: Any, **kwargs: Any) -> Any:
        return _OneBotChatWnd(*args, **kwargs)


def get_backend_name() -> str:
    return os.getenv(_BACKEND_ENV, "onebot").strip().lower() or "onebot"


def _build_backend() -> Any:
    backend_name = get_backend_name()

    if backend_name == "wxauto":
        logger.warning("微信兼容层当前显式使用 wxauto 后端")
        return _LegacyWxautoBackend()

    if backend_name == "onebot":
        logger.info("微信兼容层使用 OneBot 后端")
        return _OneBotBackend()

    raise ValueError(f"不支持的微信后端: {backend_name}")


class WeChat(_BackendProxy):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        backend = _build_backend()
        super().__init__(backend.create_wechat(*args, **kwargs))
        self._backend_name = get_backend_name()


class ChatWnd(_BackendProxy):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        backend = _build_backend()
        super().__init__(backend.create_chat_wnd(*args, **kwargs))
        self._backend_name = get_backend_name()
