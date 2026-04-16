from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from teams_runtime.core.persistence import datetime_to_runtime_iso, normalize_runtime_datetime

try:
    import aiohttp
    import discord
    from discord.backoff import ExponentialBackoff
    from discord.errors import ConnectionClosed, GatewayNotFound, HTTPException, PrivilegedIntentsRequired
    from discord.gateway import DiscordWebSocket, ReconnectWebSocket
except ImportError:
    aiohttp = None
    discord = None


LOGGER = logging.getLogger(__name__)
DISCORD_SDK_LOGGER = logging.getLogger("discord.client")
SEND_MAX_ATTEMPTS = 3
SEND_RETRY_BASE_SECONDS = 0.35
_ATTACHMENT_FILENAME_SANITIZE_PATTERN = re.compile(r'[<>:"/\\\\|?*\x00-\x1f]+')


if discord is not None:
    class _ResilientDiscordSDKClient(discord.Client):
        def _current_resume_state(self) -> dict[str, Any]:
            ws = getattr(self, "ws", None)
            if ws is None:
                return {}
            state: dict[str, Any] = {}
            gateway = getattr(ws, "gateway", None)
            session_id = getattr(ws, "session_id", None)
            sequence = getattr(ws, "sequence", None)
            if gateway is not None:
                state["gateway"] = gateway
            if session_id:
                state["session"] = session_id
            if sequence is not None:
                state["sequence"] = sequence
            return state

        @staticmethod
        def _build_ws_params(
            *,
            shard_id: int | None,
            resume_state: dict[str, Any],
            resume: bool,
        ) -> dict[str, Any]:
            params: dict[str, Any] = {
                "initial": False,
                "shard_id": shard_id,
            }
            if resume and resume_state:
                params.update(resume_state)
                params["resume"] = True
            return params

        async def connect(self, *, reconnect: bool = True) -> None:
            backoff = ExponentialBackoff()
            ws_params: dict[str, Any] = {
                "initial": True,
                "shard_id": self.shard_id,
            }
            while not self.is_closed():
                try:
                    coro = DiscordWebSocket.from_client(self, **ws_params)
                    self.ws = await asyncio.wait_for(coro, timeout=60.0)
                    ws_params["initial"] = False
                    while True:
                        await self.ws.poll_event()
                except ReconnectWebSocket as exc:
                    DISCORD_SDK_LOGGER.debug("Got a request to %s the websocket.", exc.op)
                    self.dispatch("disconnect")
                    resume_state = self._current_resume_state()
                    if not resume_state:
                        DISCORD_SDK_LOGGER.warning(
                            "Discord websocket requested resume without session state; reconnecting with a fresh session."
                        )
                    ws_params = self._build_ws_params(
                        shard_id=self.shard_id,
                        resume_state=resume_state,
                        resume=exc.resume and bool(resume_state),
                    )
                    continue
                except (
                    OSError,
                    HTTPException,
                    GatewayNotFound,
                    ConnectionClosed,
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                ) as exc:
                    self.dispatch("disconnect")
                    if not reconnect:
                        await self.close()
                        if isinstance(exc, ConnectionClosed) and exc.code == 1000:
                            return
                        raise

                    if self.is_closed():
                        return

                    resume_state = self._current_resume_state()
                    if isinstance(exc, OSError) and exc.errno in (54, 10054) and resume_state:
                        ws_params = self._build_ws_params(
                            shard_id=self.shard_id,
                            resume_state=resume_state,
                            resume=True,
                        )
                        continue

                    if isinstance(exc, ConnectionClosed):
                        if exc.code == 4014:
                            raise PrivilegedIntentsRequired(exc.shard_id) from None
                        if exc.code != 1000:
                            await self.close()
                            raise

                    retry = backoff.delay()
                    DISCORD_SDK_LOGGER.warning(
                        "Discord websocket reconnect scheduled in %.2fs after %s: %s",
                        retry,
                        type(exc).__name__,
                        exc,
                    )
                    await asyncio.sleep(retry)
                    if not resume_state:
                        DISCORD_SDK_LOGGER.warning(
                            "Discord websocket state is unavailable after a transport failure; retrying with a fresh session."
                        )
                    ws_params = self._build_ws_params(
                        shard_id=self.shard_id,
                        resume_state=resume_state,
                        resume=bool(resume_state),
                    )
else:
    _ResilientDiscordSDKClient = None


@dataclass(slots=True)
class DiscordAttachment:
    attachment_id: str
    filename: str
    content_type: str = ""
    size: int = 0
    url: str = ""
    saved_path: str = ""
    save_error: str = ""


@dataclass(slots=True)
class DiscordMessage:
    message_id: str
    channel_id: str
    guild_id: str | None
    author_id: str
    author_name: str
    content: str
    is_dm: bool
    mentions_bot: bool
    created_at: datetime
    attachments: tuple[DiscordAttachment, ...] = ()


def _encode_attachment_filename(filename: str) -> str:
    normalized = Path(str(filename or "").strip() or "attachment").name
    encoded = quote(normalized, safe=" -_.()[]{}")
    return encoded.strip(" .") or "attachment"


def _merge_discord_attachments(
    existing: tuple[DiscordAttachment, ...],
    incoming: tuple[DiscordAttachment, ...],
) -> tuple[DiscordAttachment, ...]:
    merged: dict[str, DiscordAttachment] = {}
    for attachment in [*existing, *incoming]:
        key = str(attachment.attachment_id or "").strip() or attachment.filename
        if not key:
            continue
        current = merged.get(key)
        if current is None:
            merged[key] = DiscordAttachment(
                attachment_id=attachment.attachment_id,
                filename=attachment.filename,
                content_type=attachment.content_type,
                size=attachment.size,
                url=attachment.url,
                saved_path=attachment.saved_path,
                save_error=attachment.save_error,
            )
            continue
        if attachment.saved_path and not current.saved_path:
            current.saved_path = attachment.saved_path
        if attachment.save_error and not current.save_error:
            current.save_error = attachment.save_error
        if attachment.url and not current.url:
            current.url = attachment.url
        if attachment.content_type and not current.content_type:
            current.content_type = attachment.content_type
        if attachment.size and not current.size:
            current.size = attachment.size
    return tuple(merged.values())


def _download_attachment_to_path(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=30) as response, destination.open("wb") as handle:
        handle.write(response.read())


class DiscordClientError(RuntimeError):
    """Base error for Discord client failures."""


class DiscordConfigurationError(DiscordClientError):
    """Raised when the client is misconfigured."""


class DiscordValidationError(DiscordClientError):
    """Raised when message input is invalid."""


class DiscordSendError(DiscordClientError):
    """Raised when message sending fails."""

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 1,
        last_error: BaseException | None = None,
        retryable: bool = False,
        phase: str = "",
    ) -> None:
        super().__init__(message)
        self.attempts = max(1, int(attempts or 1))
        self.last_error = last_error
        self.retryable = bool(retryable)
        self.phase = str(phase or "").strip()


class DiscordListenError(DiscordClientError):
    """Raised when listener startup or callback handling fails."""


MessageHandler = Callable[[DiscordMessage], Awaitable[None]]
ReadyHandler = Callable[[], Awaitable[None] | None]
AttachmentDirResolver = Callable[[DiscordMessage], str | Path | None]

CHUNK_MARKER_PATTERN = re.compile(r"^(.*?)(?:\[(\d+)/(\d+)\]\n)(.*)$", re.DOTALL)
MESSAGE_START_MARKER = "----------------------------------"
MESSAGE_END_MARKER = "----------------------------------"
LEADING_MENTION_PREFIX_PATTERN = re.compile(r"^(<@!?\d+>\s*\n)")


def classify_discord_exception(
    exc: BaseException | None,
    *,
    token_env_name: str = "",
    expected_bot_id: str = "",
) -> dict[str, str]:
    message = str(exc or "").strip()
    lowered = message.lower()
    token_hint = token_env_name or "DISCORD_TOKEN"
    expected_hint = str(expected_bot_id or "").strip()

    if "discord.py is not installed" in lowered:
        return {
            "category": "discord_sdk_missing",
            "summary": message or "discord.py is not installed.",
            "recovery_action": "현재 런타임 파이썬 환경에 teams_runtime/requirements.txt 의존성을 설치하거나 올바른 환경에서 서비스를 다시 시작합니다.",
        }
    if "token is not set" in lowered:
        return {
            "category": "token_missing",
            "summary": message or f"Discord token is not set for {token_hint}.",
            "recovery_action": f"{token_hint} 값을 설정한 뒤 서비스를 재시작합니다.",
        }
    if "identity mismatch" in lowered:
        expected_message = f"expected bot_id {expected_hint}" if expected_hint else "expected bot identity"
        return {
            "category": "identity_mismatch",
            "summary": message or f"Discord client identity mismatch: {expected_message}.",
            "recovery_action": "토큰이 올바른 봇 계정인지 확인한 뒤 서비스를 재시작합니다.",
        }
    if "client disconnected" in lowered or "not connected" in lowered:
        return {
            "category": "client_disconnected",
            "summary": message or "Discord client disconnected.",
            "recovery_action": "자동 재연결을 기다리거나 서비스를 재시작합니다.",
        }
    dns_error_markers = (
        "nodename nor servname provided",
        "name or service not known",
        "temporary failure in name resolution",
        "getaddrinfo failed",
        "clientconnectordnserror",
    )
    if (aiohttp is not None and isinstance(exc, getattr(aiohttp, "ClientConnectorDNSError", tuple()))) or any(
        marker in lowered for marker in dns_error_markers
    ):
        return {
            "category": "discord_dns_failed",
            "summary": message or "Discord DNS lookup failed.",
            "recovery_action": "DNS 또는 외부 네트워크 상태를 확인한 뒤 자동 재시도를 기다리거나 잠시 후 다시 시도합니다.",
        }
    if isinstance(exc, asyncio.TimeoutError) or "timeout" in lowered:
        return {
            "category": "discord_timeout",
            "summary": message or "Discord connection attempt timed out.",
            "recovery_action": "네트워크 또는 Discord gateway 상태를 확인한 뒤 자동 재시도를 기다리거나 서비스를 재시작합니다.",
        }
    if "privileged intents" in lowered:
        return {
            "category": "privileged_intents_required",
            "summary": message or "Discord privileged intents are required.",
            "recovery_action": "Discord Developer Portal에서 필요한 intents를 활성화한 뒤 서비스를 재시작합니다.",
        }
    if "improper token" in lowered or "401" in lowered or "login failure" in lowered:
        return {
            "category": "login_failed",
            "summary": message or "Discord login failed.",
            "recovery_action": f"{token_hint} 값이 유효한지 확인한 뒤 서비스를 재시작합니다.",
        }
    return {
        "category": "discord_connection_failed",
        "summary": message or "Discord connection failed.",
        "recovery_action": "런타임 로그를 확인하고 네트워크 또는 Discord API 상태를 점검한 뒤 재시도합니다.",
    }


def strip_message_boundary_markers(content: str) -> str:
    text = str(content or "")
    leading_prefix = ""
    mention_match = LEADING_MENTION_PREFIX_PATTERN.match(text)
    if mention_match:
        leading_prefix = mention_match.group(1)
        text = text[len(leading_prefix) :]
    wrapped_prefix = f"{MESSAGE_START_MARKER}\n"
    wrapped_suffix = f"\n{MESSAGE_END_MARKER}"
    if text.startswith(wrapped_prefix) and text.endswith(wrapped_suffix):
        return leading_prefix + text[len(wrapped_prefix) : -len(wrapped_suffix)]
    if text.startswith(MESSAGE_START_MARKER) and text.endswith(MESSAGE_END_MARKER):
        return leading_prefix + text[len(MESSAGE_START_MARKER) : -len(MESSAGE_END_MARKER)]
    return leading_prefix + text


def _nearest_dotenv_path(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).expanduser().resolve()
    for candidate_dir in (current, *current.parents):
        candidate = candidate_dir / ".env"
        if candidate.is_file():
            return candidate
    return None


class DiscordClient:
    def __init__(
        self,
        *,
        token: str | None = None,
        token_env: str | None = None,
        expected_bot_id: str | None = None,
        allowed_bot_author_ids: set[str] | None = None,
        always_listen_channel_ids: set[str] | None = None,
        transcript_log_file: str | Path | None = None,
        attachment_dir: str | Path | None = None,
        attachment_dir_resolver: AttachmentDirResolver | None = None,
        client_name: str | None = None,
    ):
        dotenv_path = _nearest_dotenv_path()
        if dotenv_path is not None:
            load_dotenv(dotenv_path=dotenv_path, override=True)
        else:
            load_dotenv(override=True)
        env_name = str(token_env or "").strip()
        env_token = str(os.getenv(env_name) or "").strip() if env_name else ""
        self._token = str(token or env_token or os.getenv("DISCORD_TOKEN") or "").strip()
        if not self._token:
            missing = env_name or "DISCORD_TOKEN"
            raise DiscordConfigurationError(
                f"Discord token is not set. Configure {missing} or pass token explicitly."
            )
        if discord is None:
            raise DiscordConfigurationError(
                "discord.py is not installed. Install teams_runtime/requirements.txt."
            )
        self._token_env_name = env_name
        self._expected_bot_id = str(expected_bot_id or "").strip()
        self._allowed_bot_author_ids = {str(item) for item in (allowed_bot_author_ids or set())}
        self._always_listen_channel_ids = {str(item) for item in (always_listen_channel_ids or set())}
        self._transcript_log_file = (
            Path(transcript_log_file).expanduser().resolve() if transcript_log_file else None
        )
        self._attachment_dir = Path(attachment_dir).expanduser().resolve() if attachment_dir else None
        self._attachment_dir_resolver = attachment_dir_resolver
        self._client_name = str(client_name or "").strip()
        self._client: Any | None = None
        self._listening = False
        self._startup_error: DiscordClientError | None = None
        self._ready_event: asyncio.Event | None = None
        self._message_handler: MessageHandler | None = None
        self._ready_handler: ReadyHandler | None = None
        self._send_lock: asyncio.Lock | None = None
        self._last_send_time: float = 0.0
        self._chunk_buffers: dict[tuple[str, str], dict[str, Any]] = {}

    async def send_channel_message(self, channel_id: str | int, content: str) -> DiscordMessage:
        channel_snowflake = self._normalize_snowflake(channel_id, "channel_id")
        message_content = self._validate_content(content)

        async def operation(client: Any) -> DiscordMessage:
            try:
                channel = await client.fetch_channel(channel_snowflake)
            except Exception as exc:
                raise self._wrap_send_exception(
                    phase=f"fetch_channel({channel_snowflake})",
                    exc=exc,
                ) from exc
            if not hasattr(channel, "send"):
                raise DiscordSendError(f"Channel {channel_snowflake} does not support sending messages.")
            try:
                sent_message = await channel.send(message_content)
            except Exception as exc:
                raise self._wrap_send_exception(
                    phase=f"channel.send({channel_snowflake})",
                    exc=exc,
                ) from exc
            return self._to_discord_message(sent_message)

        try:
            result = await self._execute_send(operation)
        except DiscordSendError as exc:
            self._append_transcript_event(
                direction="outbound",
                transport="channel",
                status="failed",
                target_id=str(channel_snowflake),
                content=message_content,
                error=str(exc),
                metadata=self._build_send_error_metadata(exc),
            )
            raise
        self._append_transcript_event(
            direction="outbound",
            transport="channel",
            status="sent",
            target_id=str(channel_snowflake),
            content=message_content,
            message=result,
        )
        return result

    async def send_dm(self, user_id: str | int, content: str) -> DiscordMessage:
        user_snowflake = self._normalize_snowflake(user_id, "user_id")
        message_content = self._validate_content(content)

        async def operation(client: Any) -> DiscordMessage:
            try:
                user = await client.fetch_user(user_snowflake)
            except Exception as exc:
                raise self._wrap_send_exception(
                    phase=f"fetch_user({user_snowflake})",
                    exc=exc,
                ) from exc
            try:
                dm_channel = getattr(user, "dm_channel", None) or await user.create_dm()
            except Exception as exc:
                raise self._wrap_send_exception(
                    phase=f"create_dm({user_snowflake})",
                    exc=exc,
                ) from exc
            if dm_channel is None or not hasattr(dm_channel, "send"):
                raise DiscordSendError(f"Unable to create DM channel for user {user_snowflake}.")
            try:
                sent_message = await dm_channel.send(message_content)
            except Exception as exc:
                raise self._wrap_send_exception(
                    phase=f"dm.send({user_snowflake})",
                    exc=exc,
                ) from exc
            return self._to_discord_message(sent_message)

        try:
            result = await self._execute_send(operation)
        except DiscordSendError as exc:
            self._append_transcript_event(
                direction="outbound",
                transport="dm",
                status="failed",
                target_id=str(user_snowflake),
                content=message_content,
                error=str(exc),
                metadata=self._build_send_error_metadata(exc),
            )
            raise
        self._append_transcript_event(
            direction="outbound",
            transport="dm",
            status="sent",
            target_id=str(user_snowflake),
            content=message_content,
            message=result,
        )
        return result

    async def listen(self, on_message: MessageHandler, on_ready: ReadyHandler | None = None) -> None:
        if self._listening:
            raise DiscordListenError("Discord listener is already running.")
        if not callable(on_message):
            raise DiscordValidationError("on_message must be callable.")

        self._message_handler = on_message
        self._ready_handler = on_ready
        self._client, self._ready_event = self._build_sdk_client()
        self._listening = True
        self._startup_error = None
        try:
            await self._client.start(self._token)
            if self._startup_error is not None:
                raise self._startup_error
        except DiscordClientError:
            raise
        except Exception as exc:
            raise DiscordListenError(f"Discord listener failed: {exc}") from exc
        finally:
            await self.close()

    async def close(self) -> None:
        client = self._client
        self._listening = False
        self._message_handler = None
        self._ready_handler = None
        self._ready_event = None
        self._client = None
        if client is not None:
            try:
                await client.close()
            except Exception as exc:  # pragma: no cover
                LOGGER.warning("Error while closing Discord client: %s", exc)

    def _build_sdk_client(self) -> tuple[Any, asyncio.Event]:
        intents = discord.Intents.default()
        for attr in ("guilds", "messages", "guild_messages", "dm_messages"):
            if hasattr(intents, attr):
                setattr(intents, attr, True)
        # `teams_runtime` only relies on DMs, mentions, and relay-channel messages
        # that explicitly mention the target bot. Those flows do not require
        # privileged message-content intent to be enabled by default.
        if hasattr(intents, "message_content"):
            intents.message_content = self._use_privileged_message_content_intent()

        ready_event = asyncio.Event()
        client_factory = discord.Client
        if _ResilientDiscordSDKClient is not None:
            base_client_class = _ResilientDiscordSDKClient.__mro__[1]
            if getattr(discord, "Client", None) is base_client_class:
                client_factory = _ResilientDiscordSDKClient
        client = client_factory(intents=intents)

        @client.event
        async def on_ready() -> None:
            bot_name = getattr(client.user, "name", "unknown")
            bot_id = str(getattr(client.user, "id", "") or "")
            if self._expected_bot_id and bot_id and bot_id != self._expected_bot_id:
                token_hint = self._token_env_name or "DISCORD_TOKEN"
                self._startup_error = DiscordListenError(
                    "Discord client identity mismatch for "
                    f"{self._client_name or 'unknown'}: expected bot_id {self._expected_bot_id}, "
                    f"got {bot_id} ({bot_name}) from {token_hint}."
                )
                LOGGER.error("%s", self._startup_error)
                await client.close()
                return
            ready_event.set()
            LOGGER.info(
                "Discord client ready as %s (%s) for role %s [expected_bot_id=%s]",
                bot_name,
                bot_id or "unknown",
                self._client_name or "unknown",
                self._expected_bot_id or "unknown",
            )
            if self._ready_handler is None:
                return
            try:
                result = self._ready_handler()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # pragma: no cover
                LOGGER.warning("Startup ready handler failed: %s", exc)

        @client.event
        async def on_message(message: Any) -> None:
            await self._dispatch_message(message)

        return client, ready_event

    @staticmethod
    def _use_privileged_message_content_intent() -> bool:
        raw = str(os.getenv("TEAMS_RUNTIME_ENABLE_MESSAGE_CONTENT_INTENT") or "").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _process_incoming_chunk(self, message: DiscordMessage) -> DiscordMessage | None:
        content = message.content
        match = CHUNK_MARKER_PATTERN.match(content)
        if not match or len(match.group(1)) > 200:
            return message

        prefix = match.group(1)
        current = int(match.group(2))
        total = int(match.group(3))
        body = match.group(4)

        if total <= 1:
            message.content = prefix + body
            return message

        key = (message.channel_id, message.author_id)
        now = time.monotonic()

        to_delete = [k for k, v in self._chunk_buffers.items() if now - v["updated_at"] > 60]
        for k in to_delete:
            del self._chunk_buffers[k]

        buffer = self._chunk_buffers.get(key)
        if buffer is None or buffer["total"] != total:
            buffer = {
                "total": total,
                "chunks": {},
                "prefix": prefix,
                "updated_at": now,
                "attachments": (),
            }
            self._chunk_buffers[key] = buffer

        buffer["chunks"][current] = body
        buffer["updated_at"] = now
        buffer["attachments"] = _merge_discord_attachments(
            tuple(buffer.get("attachments") or ()),
            message.attachments,
        )

        if len(buffer["chunks"]) == buffer["total"]:
            merged_body = "\n".join(buffer["chunks"].get(i, "") for i in range(1, total + 1))
            message.content = buffer["prefix"] + merged_body
            message.attachments = tuple(buffer.get("attachments") or ())
            del self._chunk_buffers[key]
            return message

        return None

    async def _materialize_attachments(self, message: DiscordMessage) -> DiscordMessage:
        if not message.attachments:
            return message
        if self._attachment_dir is None and self._attachment_dir_resolver is None:
            return message
        await asyncio.to_thread(self._materialize_attachments_sync, message)
        return message

    def _materialize_attachments_sync(self, message: DiscordMessage) -> DiscordMessage:
        attachment_dir = self._resolve_attachment_dir(message)
        if attachment_dir is None:
            return message
        for attachment in message.attachments:
            if attachment.saved_path and Path(attachment.saved_path).is_file():
                continue
            try:
                if not attachment.url:
                    raise ValueError(f"Attachment '{attachment.filename}' is missing a download URL.")
                destination = attachment_dir / f"{attachment.attachment_id}_{_encode_attachment_filename(attachment.filename)}"
                _download_attachment_to_path(attachment.url, destination)
                attachment.saved_path = str(destination.resolve())
                attachment.save_error = ""
            except Exception as exc:
                attachment.save_error = str(exc)
        return message

    def _resolve_attachment_dir(self, message: DiscordMessage) -> Path | None:
        resolved = self._attachment_dir
        if self._attachment_dir_resolver is not None:
            candidate = self._attachment_dir_resolver(message)
            if candidate:
                resolved = Path(candidate).expanduser().resolve()
        return resolved

    async def _dispatch_message(self, message: Any) -> None:
        if self._message_handler is None:
            return
        if not self._should_forward_message(message):
            return
        hydrated_message = await self._hydrate_message_for_listener(message)
        normalized_message = self._to_discord_message(hydrated_message)
        await self._materialize_attachments(normalized_message)

        merged_message = self._process_incoming_chunk(normalized_message)
        if merged_message is None:
            return

        merged_message.content = strip_message_boundary_markers(merged_message.content)

        self._append_transcript_event(
            direction="inbound",
            transport="dm" if merged_message.is_dm else "channel",
            status="received",
            target_id=merged_message.channel_id,
            content=merged_message.content,
            message=merged_message,
        )
        try:
            result = self._message_handler(merged_message)
            if not inspect.isawaitable(result):
                raise DiscordListenError("on_message must be an async callback.")
            await result
        except Exception as exc:
            self._append_transcript_event(
                direction="inbound",
                transport="dm" if merged_message.is_dm else "channel",
                status="callback_failed",
                target_id=merged_message.channel_id,
                content=merged_message.content,
                message=merged_message,
                error=str(exc),
            )
            LOGGER.exception("Discord message callback failed: %s", exc)

    async def _hydrate_message_for_listener(self, message: Any) -> Any:
        current_attachments = getattr(message, "attachments", None) or ()
        if current_attachments:
            return message
        channel = getattr(message, "channel", None)
        fetch_message = getattr(channel, "fetch_message", None)
        message_id = getattr(message, "id", None)
        if channel is None or message_id is None or not callable(fetch_message):
            return message
        try:
            hydrated = await fetch_message(message_id)
        except Exception as exc:
            LOGGER.warning(
                "Failed to refetch Discord message %s for attachment hydration: %s",
                message_id,
                exc,
            )
            return message
        return hydrated or message

    def _should_forward_message(self, message: Any) -> bool:
        author = getattr(message, "author", None)
        channel = getattr(message, "channel", None)
        if author is None or channel is None:
            return False
        bot_user = getattr(self._client, "user", None)
        author_id = str(getattr(author, "id", ""))
        if bot_user is not None and author_id == str(getattr(bot_user, "id", "")):
            return False

        if getattr(author, "bot", False):
            if author_id not in self._allowed_bot_author_ids:
                return False
            channel_id = str(getattr(channel, "id", ""))
            return channel_id in self._always_listen_channel_ids or self._message_mentions_bot(message)

        if getattr(message, "guild", None) is None:
            return True
        return self._message_mentions_bot(message)

    def _message_mentions_bot(self, message: Any) -> bool:
        bot_user = getattr(self._client, "user", None)
        bot_id = getattr(bot_user, "id", None)
        if bot_id is None:
            return False
        for mentioned_user in getattr(message, "mentions", []) or []:
            if getattr(mentioned_user, "id", None) == bot_id:
                return True
        return False

    def _to_discord_message(self, message: Any) -> DiscordMessage:
        guild = getattr(message, "guild", None)
        author = getattr(message, "author", None)
        channel = getattr(message, "channel", None)
        created_at = normalize_runtime_datetime(
            getattr(message, "created_at", None) or datetime.now(timezone.utc)
        )
        if author is None or channel is None:
            raise DiscordClientError("Discord message is missing author or channel information.")
        return DiscordMessage(
            message_id=str(getattr(message, "id", "")),
            channel_id=str(getattr(channel, "id", "")),
            guild_id=str(getattr(guild, "id", "")) if guild is not None else None,
            author_id=str(getattr(author, "id", "")),
            author_name=(
                getattr(author, "display_name", None)
                or getattr(author, "name", None)
                or str(getattr(author, "id", ""))
            ),
            content=str(getattr(message, "content", "")),
            is_dm=guild is None,
            mentions_bot=self._message_mentions_bot(message),
            created_at=created_at,
            attachments=tuple(
                self._to_discord_attachment(item)
                for item in (getattr(message, "attachments", None) or [])
            ),
        )

    @staticmethod
    def _to_discord_attachment(attachment: Any) -> DiscordAttachment:
        attachment_id = str(getattr(attachment, "id", "") or "").strip()
        filename = str(getattr(attachment, "filename", "") or "").strip()
        return DiscordAttachment(
            attachment_id=attachment_id or filename or "attachment",
            filename=filename or "attachment",
            content_type=str(getattr(attachment, "content_type", "") or "").strip(),
            size=int(getattr(attachment, "size", 0) or 0),
            url=(
                str(getattr(attachment, "url", "") or "").strip()
                or str(getattr(attachment, "proxy_url", "") or "").strip()
            ),
        )

    async def _execute_send(
        self,
        operation: Callable[[Any], Awaitable[DiscordMessage]],
    ) -> DiscordMessage:
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()

        async with self._send_lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            elapsed = now - self._last_send_time
            if elapsed < 1.2:
                await asyncio.sleep(1.2 - elapsed)

            try:
                last_error: DiscordSendError | None = None
                for attempt in range(1, SEND_MAX_ATTEMPTS + 1):
                    try:
                        if self._listening and self._client is not None:
                            if self._ready_event is not None:
                                await self._ready_event.wait()
                            return await operation(self._client)

                        temporary_client, _ = self._build_sdk_client()
                        try:
                            await temporary_client.login(self._token)
                            return await operation(temporary_client)
                        finally:
                            try:
                                await temporary_client.close()
                            except Exception as exc:  # pragma: no cover
                                LOGGER.warning("Error while closing temporary Discord client: %s", exc)
                    except DiscordSendError as exc:
                        last_error = self._finalize_send_error(exc, attempt=attempt)
                    except Exception as exc:
                        last_error = self._wrap_send_exception(
                            phase="send",
                            exc=exc,
                            attempt=attempt,
                        )

                    if last_error is None:
                        continue
                    if not last_error.retryable or attempt >= SEND_MAX_ATTEMPTS:
                        raise last_error

                    delay = min(SEND_RETRY_BASE_SECONDS * attempt, 1.5)
                    LOGGER.warning(
                        "Retrying Discord send in %.2fs after %s/%s failed during %s: %s",
                        delay,
                        attempt,
                        SEND_MAX_ATTEMPTS,
                        last_error.phase or "send",
                        last_error,
                    )
                    await asyncio.sleep(delay)

                if last_error is not None:
                    raise last_error
                raise DiscordSendError("Discord send operation failed without a captured error.")
            finally:
                self._last_send_time = loop.time()

    def _is_retryable_send_exception(self, exc: BaseException) -> bool:
        if isinstance(exc, DiscordSendError):
            if exc.last_error is not None:
                return self._is_retryable_send_exception(exc.last_error)
            return exc.retryable
        if isinstance(exc, asyncio.TimeoutError):
            return True
        if isinstance(exc, OSError):
            return True
        if aiohttp is not None and isinstance(exc, aiohttp.ClientError):
            return True
        if discord is not None and isinstance(exc, ConnectionClosed):
            return True
        if discord is not None and isinstance(exc, HTTPException):
            status = getattr(exc, "status", None)
            if status == 429:
                return True
            if isinstance(status, int) and status >= 500:
                return True
        return False

    def _wrap_send_exception(
        self,
        *,
        phase: str,
        exc: BaseException,
        attempt: int = 1,
    ) -> DiscordSendError:
        if isinstance(exc, DiscordSendError):
            return self._finalize_send_error(exc, attempt=attempt, default_phase=phase)
        detail = f"{type(exc).__name__}: {exc}"
        return DiscordSendError(
            f"Discord send operation failed during {phase} after {attempt} attempt(s): {detail}",
            attempts=attempt,
            last_error=exc,
            retryable=self._is_retryable_send_exception(exc),
            phase=phase,
        )

    def _finalize_send_error(
        self,
        error: DiscordSendError,
        *,
        attempt: int,
        default_phase: str = "",
    ) -> DiscordSendError:
        phase = error.phase or default_phase or "send"
        last_error = error.last_error
        detail_source = last_error if last_error is not None else error
        detail = f"{type(detail_source).__name__}: {detail_source}"
        return DiscordSendError(
            f"Discord send operation failed during {phase} after {attempt} attempt(s): {detail}",
            attempts=attempt,
            last_error=last_error or error,
            retryable=error.retryable,
            phase=phase,
        )

    @staticmethod
    def _build_send_error_metadata(error: DiscordSendError) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "attempts": int(error.attempts),
            "retryable": bool(error.retryable),
        }
        if error.phase:
            metadata["phase"] = error.phase
        if error.last_error is not None:
            metadata["last_error_type"] = type(error.last_error).__name__
            metadata["last_error"] = str(error.last_error)
        return metadata

    @staticmethod
    def _normalize_snowflake(value: str | int, field_name: str) -> int:
        if isinstance(value, int):
            if value > 0:
                return value
            raise DiscordValidationError(f"{field_name} must be a positive Discord snowflake.")
        normalized = str(value or "").strip()
        if normalized.isdigit():
            return int(normalized)
        raise DiscordValidationError(f"{field_name} must be a non-empty numeric Discord snowflake.")

    @staticmethod
    def _validate_content(content: str) -> str:
        normalized = str(content or "").strip()
        if not normalized:
            raise DiscordValidationError("content must not be empty.")
        if len(normalized) > 2000:
            raise DiscordValidationError("content must be 2000 characters or fewer.")
        return normalized

    @staticmethod
    def _normalize_attachment_path(file_path: str | Path) -> Path:
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise DiscordValidationError("file_path must point to an existing file.")
        return path

    def _append_transcript_event(
        self,
        *,
        direction: str,
        transport: str,
        status: str,
        target_id: str,
        content: str,
        message: DiscordMessage | None = None,
        error: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._transcript_log_file is None:
            return
        payload: dict[str, Any] = {
            "timestamp": datetime_to_runtime_iso(None),
            "client": self._client_name or "unknown",
            "direction": direction,
            "transport": transport,
            "status": status,
            "target_id": str(target_id),
            "content": str(content),
        }
        if message is not None:
            payload["message"] = {
                "message_id": message.message_id,
                "channel_id": message.channel_id,
                "guild_id": message.guild_id,
                "author_id": message.author_id,
                "author_name": message.author_name,
                "is_dm": message.is_dm,
                "mentions_bot": message.mentions_bot,
                "created_at": datetime_to_runtime_iso(message.created_at),
                "attachments": [
                    {
                        "attachment_id": item.attachment_id,
                        "filename": item.filename,
                        "content_type": item.content_type,
                        "size": item.size,
                        "url": item.url,
                        "saved_path": item.saved_path,
                        "save_error": item.save_error,
                    }
                    for item in message.attachments
                ],
            }
        if error:
            payload["error"] = error
        if metadata:
            payload["metadata"] = dict(metadata)
        self._transcript_log_file.parent.mkdir(parents=True, exist_ok=True)
        with self._transcript_log_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def current_identity(self) -> dict[str, str]:
        bot_user = getattr(self._client, "user", None)
        if bot_user is None:
            return {}
        return {
            "id": str(getattr(bot_user, "id", "") or "").strip(),
            "name": str(getattr(bot_user, "name", "") or "").strip(),
        }
