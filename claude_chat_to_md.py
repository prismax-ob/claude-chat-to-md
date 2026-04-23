#!/usr/bin/env python3
"""Convert Claude Code chat sessions (.jsonl) to Markdown.

Reads session JSONL files from ~/.claude/projects/ and produces clean,
readable Markdown — including subagent conversations.

By default --list shows only active conversations. Archived and deleted
sessions are hidden unless opted back in.

Usage:
    # List active sessions
    claude-chat-to-md --list

    # Include archived and/or deleted sessions in the list
    claude-chat-to-md --list --show-archived
    claude-chat-to-md --list --show-deleted
    claude-chat-to-md --list --show-archived --show-deleted

    # Convert a specific session (by UUID prefix, index, or title substring)
    claude-chat-to-md 2354ca15

    # Convert the most recent session for a project
    claude-chat-to-md --latest --project myapp

    # Convert all sessions into a directory
    claude-chat-to-md --all -d ./exported

    # Output to a specific file
    claude-chat-to-md 2354ca15 -o chat.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO


CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"


def _desktop_session_roots() -> list[Path]:
    """Return every existing Claude Desktop ``claude-code-sessions`` dir.

    Claude Desktop can be installed in several forms and a user may have
    more than one at once. Each keeps per-session metadata under a
    ``claude-code-sessions`` subdirectory of its app-data folder:

        Windows standard:  %APPDATA%\\Claude
        Windows MSIX/Store: %LOCALAPPDATA%\\Packages\\Claude_<hash>\\LocalCache\\Roaming\\Claude
        macOS:              ~/Library/Application Support/Claude
        Linux:              ~/.config/Claude

    Only roots that actually exist on disk are returned.
    """
    bases: list[Path] = []
    if sys.platform == "win32":
        if appdata := os.environ.get("APPDATA"):
            bases.append(Path(appdata) / "Claude")
        if local_appdata := os.environ.get("LOCALAPPDATA"):
            # MSIX package folder is Claude_<publisher hash>; the hash
            # varies per install, so glob for any match.
            bases.extend(
                (Path(local_appdata) / "Packages").glob(
                    "Claude_*/LocalCache/Roaming/Claude"
                )
            )
    elif sys.platform == "darwin":
        bases.append(Path.home() / "Library" / "Application Support" / "Claude")
    else:
        bases.append(Path.home() / ".config" / "Claude")

    return [root for b in bases if (root := b / "claude-code-sessions").is_dir()]


def desktop_session_states() -> dict[str, bool] | None:
    """CLI session IDs that Claude Desktop tracks, mapped to ``isArchived``.

    The desktop app writes one ``local_*.json`` per tracked session; each
    carries a ``cliSessionId`` pointing at the ``.jsonl`` under
    ``~/.claude/projects/``, plus an ``isArchived`` boolean set when the
    user archives the conversation.

    Sessions on disk but absent from this mapping were deleted from the UI
    (the metadata file is removed on delete, but the ``.jsonl`` stays).

    Returns ``None`` when no desktop install is detected (pure-CLI user);
    callers should then fall back to showing every on-disk ``.jsonl``.
    """
    roots = _desktop_session_roots()
    if not roots:
        return None
    states: dict[str, bool] = {}
    for root in roots:
        for meta_file in root.rglob("local_*.json"):
            try:
                obj = json.loads(meta_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if cli_id := obj.get("cliSessionId"):
                states[cli_id] = bool(obj.get("isArchived"))
    return states


@dataclass
class SessionInfo:
    """Metadata about a discovered session."""

    path: Path
    session_id: str
    project_path: str
    title: str | None = None
    timestamp: str | None = None
    subagent_dir: Path | None = None

    @property
    def display_project(self) -> str:
        """Human-readable project path.

        Claude Code encodes project paths by replacing '/' with '-',
        e.g. '/Users/nick/my-project' becomes '-Users-nick-my-project'.
        We reverse this by splitting on the known leading pattern and
        reconstructing path separators only where they originally were.
        """
        # The encoded path starts with '-' (representing the leading '/')
        # and uses '-' for every '/' in the original path. However, actual
        # directory names may contain hyphens too. We can't perfectly reverse
        # this, but we can use os.sep knowledge: the original path segments
        # are valid directory names. We try to find the longest valid prefix.
        raw = self.project_path
        if not raw:
            return raw

        # Try to find the actual path on disk by checking ~/.claude/projects/
        # Fall back to a heuristic: replace leading '-' with '/' and try to
        # reconstruct by checking which splits produce real-looking paths.
        # Best heuristic: the encoded form is the absolute path with '/' -> '-'
        # and a leading '-'. Try to resolve by checking if the path exists.
        candidate = "/" + raw[1:] if raw.startswith("-") else raw
        # Replace hyphens with '/' and check if it's a real path
        full_replace = candidate.replace("-", "/")
        if Path(full_replace).exists():
            return full_replace.lstrip("/")

        # Fallback: use a smarter reconstruction. Split by '-' and greedily
        # rejoin segments that form existing directories.
        parts = raw.lstrip("-").split("-")
        reconstructed = [parts[0]]
        for part in parts[1:]:
            # Try joining with the previous segment (hyphenated name)
            test_hyphen = "/" + "/".join(reconstructed[:-1] + [reconstructed[-1] + "-" + part])
            test_slash = "/" + "/".join(reconstructed + [part])
            if Path(test_hyphen).exists():
                reconstructed[-1] += "-" + part
            else:
                reconstructed.append(part)

        return "/".join(reconstructed)


def _scan_title_and_timestamp(jsonl_path: Path) -> tuple[str | None, str | None]:
    """Pull the display title and first timestamp from a session ``.jsonl``.

    Titles live in two kinds of records:

        {"type": "ai-title",     "aiTitle":     "..."}  # auto-generated
        {"type": "custom-title", "customTitle": "..."}  # user rename — wins

    A custom title always beats an AI title. Both may be rewritten later in
    the file (the custom one is re-emitted on every rename), so we scan to
    the end and keep the last of each kind.
    """
    ai_title: str | None = None
    custom_title: str | None = None
    timestamp: str | None = None
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                # Once we have a timestamp, only title records still matter —
                # and every title record contains the literal "-title".
                if timestamp and "-title" not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = obj.get("type")
                if kind == "ai-title":
                    ai_title = obj.get("aiTitle") or ai_title
                elif kind == "custom-title":
                    custom_title = obj.get("customTitle") or custom_title
                if not timestamp and obj.get("timestamp"):
                    timestamp = obj["timestamp"]
    except OSError:
        pass
    return custom_title or ai_title, timestamp


def discover_sessions() -> list[SessionInfo]:
    """Find all session JSONL files under ~/.claude/projects/."""
    sessions: list[SessionInfo] = []
    if not PROJECTS_DIR.exists():
        return sessions

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):
            sid = jsonl_file.stem
            # Skip subagent files
            if sid.startswith("agent-"):
                continue

            info = SessionInfo(
                path=jsonl_file,
                session_id=sid,
                project_path=project_dir.name,
            )

            # Check for subagent directory
            subagent_dir = project_dir / sid / "subagents"
            if subagent_dir.is_dir():
                info.subagent_dir = subagent_dir

            info.title, info.timestamp = _scan_title_and_timestamp(jsonl_file)

            sessions.append(info)

    sessions.sort(key=lambda s: s.timestamp or "", reverse=True)
    return sessions


def parse_messages(jsonl_path: Path) -> list[dict]:
    """Parse a JSONL file into an ordered list of message records."""
    messages = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") in ("user", "assistant"):
                messages.append(obj)
    return messages


def format_tool_use(content_block: dict) -> str:
    """Format a tool_use content block as Markdown."""
    name = content_block.get("name", "Unknown")
    inp = content_block.get("input", {})

    lines = [f"**Tool: {name}**"]

    if name == "Bash":
        cmd = inp.get("command", "")
        desc = inp.get("description", "")
        if desc:
            lines.append(f"*{desc}*")
        lines.append(f"```bash\n{cmd}\n```")
    elif name == "Read":
        fp = inp.get("file_path", "")
        lines.append(f"Reading `{fp}`")
    elif name == "Write":
        fp = inp.get("file_path", "")
        content = inp.get("content", "")
        lines.append(f"Writing `{fp}`")
        if content:
            # Show first/last lines for long files
            content_lines = content.split("\n")
            if len(content_lines) > 30:
                preview = "\n".join(content_lines[:15])
                preview += f"\n\n... ({len(content_lines) - 30} lines omitted) ...\n\n"
                preview += "\n".join(content_lines[-15:])
            else:
                preview = content
            ext = Path(fp).suffix.lstrip(".")
            lines.append(f"```{ext}\n{preview}\n```")
    elif name == "Edit":
        fp = inp.get("file_path", "")
        old = inp.get("old_string", "")
        new = inp.get("new_string", "")
        lines.append(f"Editing `{fp}`")
        if old or new:
            lines.append("```diff")
            for ol in old.split("\n"):
                lines.append(f"- {ol}")
            for nl in new.split("\n"):
                lines.append(f"+ {nl}")
            lines.append("```")
    elif name == "Grep":
        pattern = inp.get("pattern", "")
        path = inp.get("path", ".")
        lines.append(f"Searching for `{pattern}` in `{path}`")
    elif name == "Glob":
        pattern = inp.get("pattern", "")
        lines.append(f"Finding files matching `{pattern}`")
    elif name == "Agent":
        desc = inp.get("description", "")
        subtype = inp.get("subagent_type", "general-purpose")
        lines.append(f"Spawning **{subtype}** agent: *{desc}*")
        prompt = inp.get("prompt", "")
        if prompt:
            # Truncate long prompts
            if len(prompt) > 500:
                prompt = prompt[:500] + "..."
            lines.append(f"\n> {prompt}")
    elif name in ("WebSearch", "WebFetch"):
        query = inp.get("query", inp.get("url", ""))
        lines.append(f"`{query}`")
    else:
        # Generic: show input as JSON
        if inp:
            lines.append(f"```json\n{json.dumps(inp, indent=2)[:500]}\n```")

    return "\n".join(lines)


def format_tool_result(content_block: dict) -> str:
    """Format a tool_result content block as Markdown."""
    content = content_block.get("content", "")
    is_error = content_block.get("is_error", False)

    if isinstance(content, list):
        # Content can be a list of text/image blocks
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item["text"])
            elif isinstance(item, dict) and item.get("type") == "image":
                parts.append("*[image]*")
            else:
                parts.append(str(item))
        content = "\n".join(parts)

    if not content:
        return ""

    prefix = "**Error:**\n" if is_error else ""
    # Truncate very long results
    lines = content.split("\n")
    if len(lines) > 50:
        content = "\n".join(lines[:25])
        content += f"\n\n... ({len(lines) - 50} lines omitted) ...\n\n"
        content += "\n".join(lines[-25:])

    return f"{prefix}```\n{content}\n```"


def format_content(content: list | str) -> str:
    """Format a message's content array into Markdown."""
    if isinstance(content, str):
        return content

    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif not isinstance(block, dict):
            continue
        elif block.get("type") == "text":
            text = block["text"]
            # Strip IDE system tags for cleaner output
            text = re.sub(r"<ide_opened_file>.*?</ide_opened_file>", "", text, flags=re.DOTALL)
            text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL)
            text = text.strip()
            if text:
                parts.append(text)
        elif block.get("type") == "tool_use":
            parts.append(format_tool_use(block))
        elif block.get("type") == "tool_result":
            parts.append(format_tool_result(block))

    return "\n\n".join(parts)


def convert_subagent(jsonl_path: Path, meta_path: Path | None) -> str:
    """Convert a subagent session into a Markdown section."""
    meta = {}
    if meta_path and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    agent_type = meta.get("agentType", "unknown")
    description = meta.get("description", "Subagent")

    messages = parse_messages(jsonl_path)
    lines = [f"#### Subagent: {description}", f"*Type: {agent_type}*\n"]

    for msg in messages:
        role = msg.get("type", msg.get("message", {}).get("role", "unknown"))
        content = msg.get("message", {}).get("content", [])
        formatted = format_content(content)
        if not formatted:
            continue

        if role == "user":
            lines.append(f"**Prompt:**\n\n{formatted}\n")
        elif role == "assistant":
            lines.append(f"{formatted}\n")

    return "\n".join(lines)


def convert_session(
    session: SessionInfo,
    include_subagents: bool = True,
    include_tool_results: bool = True,
) -> str:
    """Convert a full session to Markdown."""
    messages = parse_messages(session.path)

    # Collect subagent data if available
    subagents: dict[str, str] = {}  # tool_use_id -> markdown
    if include_subagents and session.subagent_dir:
        for meta_file in session.subagent_dir.glob("*.meta.json"):
            agent_id = meta_file.stem.replace(".meta", "")
            jsonl_file = session.subagent_dir / f"{agent_id}.jsonl"
            if jsonl_file.exists():
                subagents[agent_id] = convert_subagent(jsonl_file, meta_file)

    # Build output
    lines = []

    # Header
    title = session.title or "Untitled Session"
    ts = session.timestamp or ""
    if ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            ts = dt.strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            pass

    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"**Session:** `{session.session_id}`  ")
    lines.append(f"**Project:** `{session.display_project}`  ")
    if ts:
        lines.append(f"**Date:** {ts}  ")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Track which agent tool_use_ids map to which subagent
    agent_tool_use_map: dict[str, str] = {}

    for msg in messages:
        role = msg.get("type", "")
        content = msg.get("message", {}).get("content", [])

        if role == "user":
            # Check if this is a tool_result (not direct user input)
            if isinstance(content, list) and all(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            ):
                if include_tool_results:
                    formatted = format_content(content)
                    if formatted.strip():
                        lines.append(f"<details><summary>Tool Result</summary>\n\n{formatted}\n\n</details>\n")
                continue

            formatted = format_content(content)
            if formatted.strip():
                # Wrap user messages in a blue-styled <div> so they're
                # visually distinct from assistant replies in renderers
                # that honor inline HTML + CSS (VS Code preview, Obsidian,
                # Typora, etc.). GitHub's renderer strips `style`
                # attributes for security, so the color is suppressed
                # there — the Markdown still renders correctly, just in
                # the default color.
                lines.append(
                    f'<div style="color: #1e6bb8">\n\n## User\n\n{formatted}\n\n</div>\n'
                )

        elif role == "assistant":
            formatted_parts = []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        if block.get("name") == "Agent":
                            # Map this tool_use to subagent output
                            desc = block.get("input", {}).get("description", "")
                            # Find matching subagent by description
                            for aid, md in subagents.items():
                                if desc and desc in md:
                                    agent_tool_use_map[block["id"]] = aid

                        formatted_parts.append(format_tool_use(block))
                    elif isinstance(block, dict) and block.get("type") == "text":
                        import re
                        text = block["text"]
                        text = re.sub(r"<ide_opened_file>.*?</ide_opened_file>", "", text, flags=re.DOTALL)
                        text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL)
                        text = text.strip()
                        if text:
                            formatted_parts.append(text)
                    elif isinstance(block, dict) and block.get("type") == "tool_result":
                        if include_tool_results:
                            r = format_tool_result(block)
                            if r:
                                formatted_parts.append(r)

            formatted = "\n\n".join(formatted_parts)
            if formatted.strip():
                lines.append(f"## Assistant\n\n{formatted}\n")

            # Insert subagent conversations after the assistant message that spawned them
            if isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("name") == "Agent"
                    ):
                        tid = block.get("id", "")
                        if tid in agent_tool_use_map:
                            aid = agent_tool_use_map[tid]
                            lines.append(f"<details><summary>Subagent Conversation</summary>\n")
                            lines.append(subagents[aid])
                            lines.append("</details>\n")
                        else:
                            # Try to find by description match
                            desc = block.get("input", {}).get("description", "")
                            for aid, md in subagents.items():
                                if desc and desc.lower() in md.lower() and aid not in agent_tool_use_map.values():
                                    lines.append(f"<details><summary>Subagent Conversation</summary>\n")
                                    lines.append(md)
                                    lines.append("</details>\n")
                                    agent_tool_use_map[tid] = aid
                                    break

    return "\n".join(lines)


def list_sessions(sessions: list[SessionInfo], out: TextIO = sys.stdout) -> None:
    """Print a table of discovered sessions."""
    if not sessions:
        print("No sessions found.", file=out)
        return

    print(f"{'#':<4} {'Date':<20} {'ID':<12} {'Title':<50} {'Project'}", file=out)
    print("-" * 120, file=out)
    for i, s in enumerate(sessions, 1):
        ts = ""
        if s.timestamp:
            try:
                dt = datetime.fromisoformat(s.timestamp.replace("Z", "+00:00"))
                ts = dt.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                ts = s.timestamp[:16]
        title = (s.title or "Untitled")[:50]
        sid = s.session_id[:10] + ".."
        proj = s.display_project
        # Shorten project path
        parts = proj.split("/")
        if len(parts) > 3:
            proj = "/".join(parts[-3:])
        print(f"{i:<4} {ts:<20} {sid:<12} {title:<50} {proj}", file=out)


def find_session(sessions: list[SessionInfo], query: str) -> SessionInfo | None:
    """Find a session by UUID prefix or index."""
    # Try as index
    try:
        idx = int(query) - 1
        if 0 <= idx < len(sessions):
            return sessions[idx]
    except ValueError:
        pass

    # Try as UUID prefix
    for s in sessions:
        if s.session_id.startswith(query):
            return s

    # Try title substring
    for s in sessions:
        if s.title and query.lower() in s.title.lower():
            return s

    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert Claude Code chat sessions to Markdown",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "session",
        nargs="?",
        help="Session UUID (prefix), index number from --list, or title substring",
    )
    parser.add_argument(
        "--list",
        "-l",
        action="store_true",
        help="List sessions (active only by default; see --show-archived, --show-deleted)",
    )
    parser.add_argument("--latest", action="store_true", help="Convert the most recent session")
    parser.add_argument("--all", action="store_true", help="Convert all sessions")
    parser.add_argument("--project", "-p", help="Filter by project path substring")
    parser.add_argument("--output", "-o", help="Output file path (default: stdout)")
    parser.add_argument(
        "--no-subagents", action="store_true", help="Exclude subagent conversations"
    )
    parser.add_argument(
        "--no-tool-results", action="store_true", help="Exclude tool results"
    )
    parser.add_argument(
        "--output-dir", "-d", help="Output directory (for --all mode)"
    )
    parser.add_argument(
        "--show-archived",
        action="store_true",
        help="Also include sessions archived in the Claude Desktop UI.",
    )
    parser.add_argument(
        "--show-deleted",
        action="store_true",
        help="Also include sessions that were deleted from the Claude "
        "Desktop UI but whose .jsonl file still exists on disk.",
    )

    args = parser.parse_args()
    sessions = discover_sessions()

    # Cross-reference with Claude Desktop's per-session metadata to filter
    # by UI state. Each session falls in one of three buckets:
    #   - active:   tracked by desktop,  isArchived is False
    #   - archived: tracked by desktop,  isArchived is True
    #   - deleted:  absent from desktop metadata (file removed on delete)
    # Active is always shown; --show-archived and --show-deleted opt each
    # of the other buckets in. If no desktop install is found, `states` is
    # None and we can't classify anything — leave the list untouched so
    # pure-CLI users still see their sessions.
    states = desktop_session_states()
    if states is not None:
        sessions = [
            s
            for s in sessions
            if (archived := states.get(s.session_id)) is False
            or (archived is True and args.show_archived)
            or (archived is None and args.show_deleted)
        ]

    if args.project:
        sessions = [s for s in sessions if args.project.lower() in s.project_path.lower()]

    if args.list:
        list_sessions(sessions)
        return

    if not args.session and not args.latest and not args.all:
        parser.print_help()
        print("\nUse --list to see available sessions.", file=sys.stderr)
        sys.exit(1)

    include_subagents = not args.no_subagents
    include_tool_results = not args.no_tool_results

    if args.all:
        out_dir = Path(args.output_dir) if args.output_dir else Path(".")
        out_dir.mkdir(parents=True, exist_ok=True)
        for s in sessions:
            md = convert_session(s, include_subagents, include_tool_results)
            ts = ""
            if s.timestamp:
                try:
                    dt = datetime.fromisoformat(s.timestamp.replace("Z", "+00:00"))
                    ts = dt.strftime("%Y-%m-%d")
                except ValueError:
                    pass
            safe_title = (s.title or "untitled").replace(" ", "-").replace("/", "-")[:50]
            filename = f"{ts}-{safe_title}.md" if ts else f"{s.session_id[:8]}-{safe_title}.md"
            out_path = out_dir / filename
            out_path.write_text(md, encoding="utf-8")
            print(f"Wrote {out_path}", file=sys.stderr)
        return

    if args.latest:
        if not sessions:
            print("No sessions found.", file=sys.stderr)
            sys.exit(1)
        session = sessions[0]
    else:
        session = find_session(sessions, args.session)
        if not session:
            print(f"Session not found: {args.session}", file=sys.stderr)
            print("Use --list to see available sessions.", file=sys.stderr)
            sys.exit(1)

    md = convert_session(session, include_subagents, include_tool_results)

    if args.output:
        Path(args.output).write_text(md, encoding="utf-8")
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(md)


if __name__ == "__main__":
    main()
