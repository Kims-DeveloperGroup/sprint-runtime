from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from teams_runtime.shared.models import RoleRuntimeConfig


SESSION_ID_PATTERN = re.compile(r"session id:\s*([0-9a-fA-F-]+)", re.IGNORECASE)
LOGGER = logging.getLogger(__name__)


def extract_json_object(text: str) -> dict[str, Any]:
    normalized = str(text or "").strip()
    if not normalized:
        raise ValueError("Empty response.")

    candidates: list[str] = [normalized]
    if normalized.startswith("```"):
        lines = normalized.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            candidates.append("\n".join(lines[1:-1]).strip())

        fenced_segments: list[str] = []
        segment_lines: list[str] = []
        in_fenced_json = False
        for line in lines:
            stripped = line.strip()
            if not in_fenced_json:
                if stripped.startswith("```"):
                    in_fenced_json = True
                    segment_lines = []
                continue
            if stripped == "```":
                fenced_segments.append("\n".join(segment_lines).strip())
                segment_lines = []
                in_fenced_json = False
                continue
            segment_lines.append(line)

        merged_fenced = "\n".join(segment for segment in fenced_segments if segment).strip()
        if merged_fenced:
            candidates.append(merged_fenced)

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload

    decoder = json.JSONDecoder()
    for index, char in enumerate(normalized):
        if char != "{":
            continue
        try:
            payload, _end = decoder.raw_decode(normalized[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("No JSON object found in response.")


class CodexRunner:
    def __init__(self, runtime_config: RoleRuntimeConfig, *, role: str = ""):
        self.runtime_config = runtime_config
        self.role = str(role or "").strip()

    def _discover_extra_writable_dirs(self, workspace: Path) -> list[str]:
        extra_dirs: list[str] = []
        seen: set[str] = set()
        for directory_name in ("workspace", "shared_workspace", ".teams_runtime"):
            candidate = workspace / directory_name
            if not candidate.exists():
                continue
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            resolved_text = str(resolved)
            if resolved != workspace and resolved_text not in seen:
                seen.add(resolved_text)
                extra_dirs.append(resolved_text)
        return extra_dirs

    def _build_command(
        self,
        *,
        workspace: Path,
        prompt: str,
        session_id: str | None,
        output_file: Path,
        bypass_sandbox: bool,
    ) -> tuple[list[str], str | None]:
        is_gemini = "gemini" in self.runtime_config.model.lower()

        if is_gemini:
            command = ["gemini"]
            if session_id:
                command.extend(["--resume", session_id])
            command.extend(["--model", self.runtime_config.model])

            for extra_dir in self._discover_extra_writable_dirs(workspace):
                command.extend(["--include-directories", extra_dir])

            if bypass_sandbox:
                command.append("--yolo")

            command.extend(["--output-format", "json"])
            command.extend(["--prompt", prompt])
            return command, None

        command = ["codex", "exec"]
        if session_id:
            command.extend(
                [
                    "resume",
                    "--model",
                    self.runtime_config.model,
                    "-o",
                    str(output_file),
                    "--skip-git-repo-check",
                ]
            )
            if bypass_sandbox:
                command.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                command.append("--full-auto")
            command.extend(["-c", f'model_reasoning_effort="{self.runtime_config.reasoning}"'])
            command.extend(["-c", 'personality="friendly"'])
            command.extend([session_id, "-"])
            return command, prompt
        command.extend(
            [
                "-",
                "--model",
                self.runtime_config.model,
                "-o",
                str(output_file),
                "--skip-git-repo-check",
                "-C",
                str(workspace),
            ]
        )
        for extra_dir in self._discover_extra_writable_dirs(workspace):
            command.extend(["--add-dir", extra_dir])
        if bypass_sandbox:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            command.append("--full-auto")
        command.extend(["-c", f'model_reasoning_effort="{self.runtime_config.reasoning}"'])
        command.extend(["-c", 'personality="friendly"'])
        return command, prompt

    def run(
        self,
        workspace: Path,
        prompt: str,
        session_id: str | None,
        *,
        bypass_sandbox: bool = False,
    ) -> tuple[str, str | None]:
        abs_workspace = workspace.expanduser().resolve()
        output_file = abs_workspace / ".teams_runtime_codex_output.txt"
        try:
            output_file.unlink()
        except FileNotFoundError:
            pass
        command, stdin_input = self._build_command(
            workspace=abs_workspace,
            prompt=prompt,
            session_id=session_id,
            output_file=output_file,
            bypass_sandbox=bypass_sandbox,
        )

        is_gemini = "gemini" in self.runtime_config.model.lower()
        env = {**os.environ, "HOME": str(Path.home())}
        if is_gemini:
            env["GEMINI_SYSTEM_MD"] = str(abs_workspace / "GEMINI.md")
            gemini_dir = abs_workspace / ".gemini"
            gemini_dir.mkdir(parents=True, exist_ok=True)
            skills_symlink = gemini_dir / "skills"
            agents_skills = abs_workspace / ".agents" / "skills"
            if agents_skills.exists() and not skills_symlink.exists():
                try:
                    skills_symlink.symlink_to(agents_skills)
                except OSError:
                    pass

        process = subprocess.run(
            command,
            cwd=str(abs_workspace),
            capture_output=True,
            input=stdin_input,
            text=True,
            env=env,
            check=False,
        )

        output = ""
        resolved_session_id = session_id

        if is_gemini:
            try:
                res_json = json.loads(process.stdout)
                output = res_json.get("response", "").strip()
                resolved_session_id = res_json.get("session_id") or res_json.get("sessionId") or session_id
                if not output and res_json.get("error"):
                    error_info = res_json.get("error")
                    output = error_info.get("message") if isinstance(error_info, dict) else str(error_info)
            except json.JSONDecodeError:
                output = process.stdout.strip() or process.stderr.strip()
        else:
            combined = "\n".join(part for part in [process.stdout.strip(), process.stderr.strip()] if part).strip()
            session_match = SESSION_ID_PATTERN.search(combined)
            resolved_session_id = session_match.group(1).strip() if session_match else session_id
            if output_file.exists():
                output = output_file.read_text(encoding="utf-8").strip()
            if not output:
                output = process.stdout.strip() or combined

        if process.returncode != 0:
            cli_name = "Gemini" if is_gemini else "Codex"
            if output:
                try:
                    extract_json_object(output)
                except ValueError:
                    raise RuntimeError(output or f"{cli_name} command failed.")
                LOGGER.warning(
                    "[%s] %s command exited with code %s but produced a valid JSON payload; preserving role result",
                    self.role,
                    cli_name,
                    process.returncode,
                )
            else:
                raise RuntimeError(f"{cli_name} command failed.")
        return output, resolved_session_id


__all__ = ["CodexRunner", "extract_json_object"]
