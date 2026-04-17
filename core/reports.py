from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import unicodedata


@dataclass(frozen=True, slots=True)
class ReportSection:
    title: str
    lines: tuple[str, ...]


def _has_meaningful_text(value: str) -> bool:
    normalized = str(value or "").strip()
    return normalized not in {"", "N/A", "없음"}


def _escape_fenced_block_content(value: str) -> str:
    return str(value or "").replace("```", "``\u200b`")


def _display_width(value: str) -> int:
    width = 0
    for char in str(value or ""):
        if char in {"\u200b", "\u200d", "\ufe0f"}:
            continue
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def _split_display_fragment(value: str, max_width: int) -> tuple[str, str]:
    text = str(value or "")
    if not text or max_width <= 0:
        return text[:1], text[1:]
    width = 0
    split_at: int | None = None
    for index, char in enumerate(text):
        char_width = _display_width(char)
        if width + char_width > max_width:
            if split_at is None:
                split_at = index if index > 0 else 1
            break
        width += char_width
        if char.isspace() or char in {"|", ",", ";", "/", ")", "]"}:
            split_at = index + 1
    else:
        return text, ""
    fragment = text[:split_at].rstrip()
    remaining = text[split_at:].lstrip()
    if fragment:
        return fragment, remaining
    return text[:1], text[1:]


def _wrap_box_line(value: str, *, max_width: int) -> list[str]:
    text = str(value or "")
    if _display_width(text) <= max_width:
        return [text]
    continuation_prefix = "  " if text.startswith("- ") else ("    " if text.startswith("  ") else "")
    lines: list[str] = []
    remaining = text
    prefix = ""
    while remaining:
        available = max(1, max_width - _display_width(prefix))
        fragment, remaining = _split_display_fragment(remaining, available)
        lines.append(f"{prefix}{fragment}".rstrip())
        prefix = continuation_prefix
    return lines or [text]


def _pad_display_width(value: str, width: int) -> str:
    padding = max(0, int(width or 0) - _display_width(value))
    return f"{value}{' ' * padding}"


def _normalize_section_lines(lines: Iterable[str] | None) -> list[str]:
    normalized: list[str] = []
    for item in lines or []:
        text = str(item or "").rstrip()
        if not text:
            normalized.append("")
            continue
        normalized.append(text)
    while normalized and not normalized[0]:
        normalized.pop(0)
    while normalized and not normalized[-1]:
        normalized.pop()
    return normalized


def render_text_box(
    title: str,
    lines: Iterable[str] | None,
    *,
    max_inner_width: int = 88,
) -> str:
    rendered_lines = _normalize_section_lines(lines)
    if not rendered_lines:
        rendered_lines = ["- 없음"]
    wrapped_lines: list[str] = []
    for line in rendered_lines:
        wrapped_lines.extend(_wrap_box_line(_escape_fenced_block_content(line), max_width=max(16, int(max_inner_width or 88))))
    inner_width = max(
        _display_width(str(title or "").strip() or "Section"),
        *(_display_width(line) for line in wrapped_lines),
        16,
    )
    border = "+" + "-" * (inner_width + 2) + "+"
    title_line = f"| {_pad_display_width(str(title or '').strip() or 'Section', inner_width)} |"
    body_lines = [f"| {_pad_display_width(line, inner_width)} |" for line in wrapped_lines]
    return "\n".join([border, title_line, border, *body_lines, border])


def render_report_sections(
    sections: Iterable[ReportSection] | None,
    *,
    max_inner_width: int = 88,
) -> str:
    rendered: list[str] = []
    for section in sections or []:
        title = str(section.title or "").strip()
        if not title:
            continue
        rendered_lines = _normalize_section_lines(section.lines)
        if not rendered_lines:
            rendered_lines = ["- 없음"]
        wrapped_lines: list[str] = []
        for line in rendered_lines:
            wrapped_lines.extend(
                _wrap_box_line(
                    _escape_fenced_block_content(line),
                    max_width=max(16, int(max_inner_width or 88)),
                )
            )
        body = "\n".join([f"[{title}]", *wrapped_lines]).strip()
        rendered.append(f"```text\n{body}\n```")
    return "\n\n".join(rendered)


def box_text_message(content: str, *, info_string: str = "", max_inner_width: int = 88) -> str:
    normalized = str(content or "").strip()
    if not normalized:
        return ""
    raw_lines: list[str] = []
    for line in _escape_fenced_block_content(normalized).splitlines() or [""]:
        raw_lines.extend(_wrap_box_line(line, max_width=max(24, int(max_inner_width or 88))))
    return "\n".join(raw_lines)


def _status_emoji(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"완료", "completed", "committed", "connected", "sent", "성공"}:
        return "✅"
    if normalized in {"진행중", "running", "executing", "started"}:
        return "⏳"
    if normalized in {"대기", "pending", "queued", "waiting"}:
        return "⏸️"
    if normalized in {"중단", "blocked", "cancelled", "canceled", "stopped"}:
        return "⛔"
    if normalized in {"실패", "failed", "error"}:
        return "⚠️"
    return "ℹ️"


def _humanize_artifact_path(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    path = Path(normalized)
    marker_names = {
        "shared_workspace",
        ".teams_runtime",
        "logs",
        "tmp",
        "operations",
        "sources",
        "sessions",
        "workspace",
    }
    parts = list(path.parts)
    for index, part in enumerate(parts):
        if part in marker_names:
            return "/".join(parts[index:])
    if len(parts) > 4:
        return "/".join(parts[-4:])
    return normalized


def _summarize_artifacts(artifacts: list[str] | None) -> str:
    normalized = [_humanize_artifact_path(str(item)) for item in (artifacts or []) if str(item).strip()]
    if not normalized:
        return ""
    preview = normalized[:3]
    if len(normalized) > 3:
        preview.append(f"외 {len(normalized) - 3}건")
    return ", ".join(preview)


def read_process_summary(pid: int | None) -> str:
    if pid is None:
        return "N/A"
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pid=,ppid=,stat=,etime=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return f"N/A ({exc})"
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[0] if lines else "N/A"


def read_runtime_log_tail(runtime_log_file: Path, max_lines: int = 20) -> str:
    try:
        lines = runtime_log_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""
    return "\n".join(lines[-max_lines:])


def _append_labeled_lines(lines: list[str], label: str, value: str, *, prefix: str = "- ") -> None:
    normalized = str(value or "").strip()
    if not _has_meaningful_text(normalized):
        return
    rows = [row.strip() for row in normalized.splitlines() if row.strip()]
    if not rows:
        return
    if len(rows) == 1:
        lines.append(f"{prefix}{label}: {rows[0]}")
        return
    lines.append(f"{prefix}{label}:")
    lines.extend(f"  {row}" for row in rows)


def build_progress_report(
    *,
    request: str,
    scope: str,
    status: str,
    list_summary: str,
    detail_summary: str,
    process_summary: str,
    log_summary: str,
    end_reason: str,
    judgment: str,
    next_action: str,
    commit_message: str = "",
    artifacts: list[str] | None = None,
    sections: list[ReportSection] | None = None,
) -> str:
    artifact_summary = _summarize_artifacts(artifacts)
    summary_text = str(judgment or detail_summary or request).strip()
    reference_lines: list[str] = []

    def append_reference(label: str, value: str) -> None:
        normalized = str(value or "").strip()
        if not _has_meaningful_text(normalized):
            return
        rows = [row.strip() for row in normalized.splitlines() if row.strip()]
        if not rows:
            return
        if len(rows) == 1:
            reference_lines.append(f"  • {label}: {rows[0]}")
            return
        reference_lines.append(f"  • {label}:")
        reference_lines.extend(f"    {row}" for row in rows)

    if not sections:
        if _has_meaningful_text(detail_summary) and str(detail_summary).strip() != summary_text:
            append_reference("상세", str(detail_summary).strip())
        append_reference("리스트", list_summary)
        append_reference("프로세스", process_summary)
        append_reference("로그", log_summary)

    lines = [
        "[작업 보고]",
        f"- 🧩 요청: {request}",
        f"- {_status_emoji(status)} 상태: {status}",
    ]
    if _has_meaningful_text(next_action):
        _append_labeled_lines(lines, "➡️ 다음", next_action)
    _append_labeled_lines(lines, "📍 범위", scope)
    if summary_text != str(request or "").strip():
        _append_labeled_lines(lines, "🧠 판단", summary_text)
    _append_labeled_lines(lines, "🧾 커밋", commit_message)
    if reference_lines:
        lines.append("- 🔎 근거:")
        lines.extend(reference_lines)
    if _has_meaningful_text(end_reason) and str(end_reason).strip() not in {summary_text, "없음"}:
        _append_labeled_lines(lines, "⚠️ 종료 사유", end_reason)
    if artifact_summary:
        _append_labeled_lines(lines, "📎 관련 아티팩트", artifact_summary)
    body_parts = [box_text_message("\n".join(lines))]
    rendered_sections = render_report_sections(sections)
    if rendered_sections:
        body_parts.append(rendered_sections)
    return "\n\n".join(part for part in body_parts if part.strip())
