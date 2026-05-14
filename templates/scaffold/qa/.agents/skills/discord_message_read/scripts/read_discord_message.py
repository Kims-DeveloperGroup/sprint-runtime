#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only in incomplete runtime envs
    yaml = None


API_BASE = "https://discord.com/api/v10"
SNOWFLAKE_PATTERN = re.compile(r"^\d+$")


class DiscordApiError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, body: str = "", url: str = "") -> None:
        super().__init__(message)
        self.status = int(status or 0)
        self.body = str(body or "")
        self.url = str(url or "")


def _resolve_path(value: str | None) -> Path:
    candidate = Path(value or ".").expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.resolve()


def _candidate_roots(workspace_root: Path) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            return
        if resolved in seen:
            return
        seen.add(resolved)
        candidates.append(resolved)

    add(workspace_root)
    for path in (Path.cwd(), *Path.cwd().parents, *workspace_root.parents):
        add(path)

    runtime_root = workspace_root / ".teams_runtime"
    if runtime_root.exists() or runtime_root.is_symlink():
        add(runtime_root.resolve().parent)

    for base in list(candidates):
        add(base / "workspace")
        add(base / "workspace" / "teams_generated")

    return candidates


def _find_config_root(workspace_root: Path) -> Path:
    for candidate in _candidate_roots(workspace_root):
        if (candidate / "discord_agents_config.yaml").is_file():
            return candidate
    raise FileNotFoundError(
        "Could not find discord_agents_config.yaml from workspace root, cwd, parents, or session runtime links."
    )


def _load_dotenv_file(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip().strip("'").strip('"')
        os.environ[key] = value


def _load_nearest_dotenv(*starts: Path) -> Path | None:
    for start in starts:
        for candidate in (start, *start.parents):
            dotenv_path = candidate / ".env"
            if not dotenv_path.is_file():
                continue
            try:
                from dotenv import load_dotenv

                load_dotenv(dotenv_path=dotenv_path, override=False)
            except ImportError:
                _load_dotenv_file(dotenv_path)
            return dotenv_path
    return None


def _load_discord_config(config_root: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is not installed. Install teams_runtime requirements before using this helper.")
    config_path = config_root / "discord_agents_config.yaml"
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{config_path} did not contain a YAML mapping.")
    return payload


def _role_token_env(config: dict[str, Any], role: str) -> str:
    agents = config.get("agents")
    if not isinstance(agents, dict):
        return ""
    role_config = agents.get(role)
    if not isinstance(role_config, dict):
        return ""
    return str(role_config.get("token_env") or "").strip()


def _resolve_token(config: dict[str, Any], *, role: str, token_env: str) -> tuple[str, str]:
    resolved_env = str(token_env or "").strip() or _role_token_env(config, role) or "DISCORD_TOKEN"
    token = str(os.getenv(resolved_env) or "").strip()
    if not token and resolved_env != "DISCORD_TOKEN":
        token = str(os.getenv("DISCORD_TOKEN") or "").strip()
    if not token:
        raise RuntimeError(f"Discord token is not set. Configure {resolved_env} or DISCORD_TOKEN.")
    return resolved_env, token


def _normalize_snowflake(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not SNOWFLAKE_PATTERN.match(normalized):
        raise ValueError(f"{field_name} must be a Discord snowflake numeric ID.")
    return normalized


def _api_get(path: str, *, token: str, query: dict[str, Any] | None = None) -> Any:
    url = f"{API_BASE}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    request = Request(
        url,
        headers={
            "Authorization": f"Bot {token}",
            "Accept": "application/json",
            "User-Agent": "teams_runtime-qa-discord-message-read",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise DiscordApiError(
            _discord_http_hint(exc.code),
            status=exc.code,
            body=body,
            url=url,
        ) from exc
    except URLError as exc:
        raise DiscordApiError(f"Discord API request failed: {exc}", url=url) from exc
    return json.loads(body) if body else None


def _discord_http_hint(status: int) -> str:
    if status == 401:
        return "Discord API rejected the bot token."
    if status == 403:
        return "Discord API denied access. Check channel visibility and READ_MESSAGE_HISTORY permissions."
    if status == 404:
        return "Discord channel or message was not found, or the bot cannot access it."
    if status == 429:
        return "Discord API rate limited the request."
    return f"Discord API returned HTTP {status}."


def _compact_user(value: Any) -> dict[str, Any]:
    user = value if isinstance(value, dict) else {}
    return {
        "id": str(user.get("id") or ""),
        "username": str(user.get("username") or ""),
        "global_name": str(user.get("global_name") or ""),
        "bot": bool(user.get("bot", False)),
    }


def _compact_message(message: dict[str, Any]) -> dict[str, Any]:
    attachments = message.get("attachments") if isinstance(message.get("attachments"), list) else []
    embeds = message.get("embeds") if isinstance(message.get("embeds"), list) else []
    components = message.get("components") if isinstance(message.get("components"), list) else []
    return {
        "id": str(message.get("id") or ""),
        "channel_id": str(message.get("channel_id") or ""),
        "guild_id": str(message.get("guild_id") or ""),
        "author": _compact_user(message.get("author")),
        "content": str(message.get("content") or ""),
        "timestamp": str(message.get("timestamp") or ""),
        "edited_timestamp": message.get("edited_timestamp"),
        "type": message.get("type"),
        "flags": message.get("flags"),
        "mention_everyone": bool(message.get("mention_everyone", False)),
        "mentions": [_compact_user(item) for item in (message.get("mentions") or []) if isinstance(item, dict)],
        "mention_roles": [str(item) for item in (message.get("mention_roles") or [])],
        "attachments": [
            {
                "id": str(item.get("id") or ""),
                "filename": str(item.get("filename") or ""),
                "content_type": str(item.get("content_type") or ""),
                "size": item.get("size"),
                "url": str(item.get("url") or ""),
            }
            for item in attachments
            if isinstance(item, dict)
        ],
        "embeds": embeds,
        "components": components,
    }


def _message_warnings(message: dict[str, Any]) -> list[str]:
    if (
        not str(message.get("content") or "")
        and not message.get("attachments")
        and not message.get("embeds")
        and not message.get("components")
    ):
        return [
            "Message content/embed/attachment/component fields are empty. If this is unexpected, verify Message Content intent and channel permissions."
        ]
    return []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read one Discord channel message for QA evidence.")
    parser.add_argument("--workspace-root", default=".", help="Generated teams_runtime workspace root or role session root.")
    parser.add_argument("--role", default="qa", help="Role whose discord_agents_config token_env should be used.")
    parser.add_argument("--token-env", default="", help="Override token environment variable name.")
    parser.add_argument("--channel-id", required=True, help="Discord channel snowflake ID.")
    parser.add_argument("--message-id", required=True, help="Discord message snowflake ID.")
    parser.add_argument(
        "--around",
        type=int,
        default=0,
        help="Read up to this many nearby channel messages around --message-id (1-100).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace_root = _resolve_path(args.workspace_root)
    try:
        config_root = _find_config_root(workspace_root)
        dotenv_path = _load_nearest_dotenv(config_root, Path.cwd())
        config = _load_discord_config(config_root)
        token_env, token = _resolve_token(config, role=str(args.role or "qa").strip(), token_env=args.token_env)
        channel_id = _normalize_snowflake(args.channel_id, "channel_id")
        message_id = _normalize_snowflake(args.message_id, "message_id")
        raw_message = _api_get(f"/channels/{channel_id}/messages/{message_id}", token=token)
        if not isinstance(raw_message, dict):
            raise ValueError("Discord API did not return a message object.")
        message = _compact_message(raw_message)

        context: list[dict[str, Any]] = []
        around = max(0, min(int(args.around or 0), 100))
        if around:
            raw_context = _api_get(
                f"/channels/{channel_id}/messages",
                token=token,
                query={"around": message_id, "limit": around},
            )
            if isinstance(raw_context, list):
                context = [_compact_message(item) for item in raw_context if isinstance(item, dict)]

        result = {
            "ok": True,
            "source": "discord_api",
            "workspace_root": str(workspace_root),
            "config_root": str(config_root),
            "dotenv_path": str(dotenv_path or ""),
            "role": str(args.role or "qa").strip(),
            "token_env": token_env,
            "channel_id": channel_id,
            "message_id": message_id,
            "message": message,
            "context": context,
            "warnings": _message_warnings(message),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except DiscordApiError as exc:
        result = {
            "ok": False,
            "source": "discord_api",
            "workspace_root": str(workspace_root),
            "http_status": exc.status,
            "url": exc.url,
            "error": str(exc),
            "body": exc.body,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    except Exception as exc:
        result = {
            "ok": False,
            "source": "discord_api",
            "workspace_root": str(workspace_root),
            "error": str(exc),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
