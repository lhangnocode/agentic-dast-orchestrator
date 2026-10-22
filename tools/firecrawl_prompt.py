"""
Builds the Firecrawl Agent prompt from a template file plus per-target context.

- Template: configs/prompts/firecrawl_agent.md, filled with `string.Template.safe_substitute`
  (`${target_url}`, `${seeds}`, `${app_context}`, `${known_routes}`, `${focus}`,
  `${auth_instructions}`, `${constraints}`), so `{`, `}` or `$` in user text never break it.
- Safety constraints are owned by this module, not the template: they replace `${constraints}`
  and are appended if the template does not contain the placeholder.
- The Agent API accepts at most 10,000 prompt characters. If the prompt is longer, the
  known-routes summary is shortened first, then the seed list (seeds are also sent as `urls`);
  the application context is only cut as a last resort, with a warning.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import List, Optional

logger = logging.getLogger("orchestrator.firecrawl")

MAX_PROMPT_CHARS = 10_000

SAFETY_CONSTRAINTS = """\
- Stay on the target's own host; do not follow links to other sites.
- Do not log out, delete data, change passwords or account settings, or start payments.
- Use only harmless test input. Never enter scripts, injection strings or other attack payloads.
- Prefer opening and reading views over submitting forms; submit only what a workflow above needs."""

TRUNCATION_NOTE = "\n(... shortened to fit the prompt limit)"

# Used when no template file is configured or found (configs/prompts/firecrawl_agent.md is the full one)
DEFAULT_TEMPLATE = """\
Map the pages and routes of the authorized application ${target_url}. Discovery only.
Start from:
${seeds}

About the application:
${app_context}

Already discovered:
${known_routes}

Focus: ${focus}

Open all menus, tabs and dialogs, follow client-side (/#/) routes and wait for each view to render.
Signing in: ${auth_instructions}

Safety rules:
${constraints}

Return every unique URL or route you observed and a short list of the actions you took.
"""


@dataclass
class PromptParts:
    target_url: str
    seeds: List[str]
    app_context: str = ""
    known_routes: str = ""
    focus: str = ""
    auth_instructions: str = ""


@dataclass
class RenderedPrompt:
    text: str
    sha256: str
    truncated: List[str] = field(default_factory=list)


def read_text_file(path: Optional[str], what: str) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.is_file():
        logger.warning(f"[FIRECRAWL] {what} file not found: {p}; using an empty section.")
        return ""
    return p.read_text(encoding="utf-8").strip()


def _seed_list(seeds: List[str], keep: Optional[int] = None) -> str:
    shown = seeds if keep is None else seeds[:keep]
    lines = [f"- {s}" for s in shown]
    if len(shown) < len(seeds):
        lines.append(f"- ... and {len(seeds) - len(shown)} more (all are listed in the request's urls)")
    return "\n".join(lines)


def _fill(template: str, parts: PromptParts, seeds_text: str, known_routes: str, app_context: str) -> str:
    text = Template(template).safe_substitute(
        target_url=parts.target_url,
        seeds=seeds_text,
        app_context=app_context or "(no application context provided)",
        known_routes=known_routes or "(nothing recorded yet)",
        focus=parts.focus or "general exploration of the application",
        auth_instructions=parts.auth_instructions or "(no login required)",
        constraints=SAFETY_CONSTRAINTS,
    )
    if SAFETY_CONSTRAINTS not in text:
        text = text.rstrip() + "\n\nSafety rules:\n" + SAFETY_CONSTRAINTS + "\n"
    return text


def render_prompt(template: str, parts: PromptParts, max_chars: int = MAX_PROMPT_CHARS) -> RenderedPrompt:
    truncated: List[str] = []
    known = parts.known_routes
    context = parts.app_context
    keep_seeds: Optional[int] = None

    text = _fill(template, parts, _seed_list(parts.seeds), known, context)
    # 1. shorten the known-routes summary (line by line, keep the start)
    while len(text) > max_chars and known:
        overflow = len(text) - max_chars + len(TRUNCATION_NOTE)
        lines = known.splitlines()
        while lines and overflow > 0:
            overflow -= len(lines.pop()) + 1
        known = ("\n".join(lines) + TRUNCATION_NOTE) if lines else ""
        if "known_routes" not in truncated:
            truncated.append("known_routes")
        text = _fill(template, parts, _seed_list(parts.seeds, keep_seeds), known, context)
    # 2. list fewer seeds in the text (they are all sent as `urls` anyway)
    while len(text) > max_chars and (keep_seeds is None or keep_seeds > 1):
        keep_seeds = max(1, (len(parts.seeds) if keep_seeds is None else keep_seeds) // 2)
        if "seeds" not in truncated:
            truncated.append("seeds")
        text = _fill(template, parts, _seed_list(parts.seeds, keep_seeds), known, context)
    # 3. last resort: the user's context
    if len(text) > max_chars and context:
        cut = max(0, len(context) - (len(text) - max_chars) - len(TRUNCATION_NOTE))
        context = context[:cut] + TRUNCATION_NOTE
        truncated.append("app_context")
        logger.warning("[FIRECRAWL] Application context was shortened to fit the 10,000-character prompt limit.")
        text = _fill(template, parts, _seed_list(parts.seeds, keep_seeds), known, context)

    if truncated:
        logger.info(f"[FIRECRAWL] Prompt shortened ({', '.join(truncated)}) to {len(text)} characters.")
    return RenderedPrompt(text=text, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), truncated=truncated)


def save_prompt(prompt: RenderedPrompt, directory: Optional[str]) -> Optional[Path]:
    """Stores the exact prompt sent, named by its hash, for experiment traceability."""
    if not directory:
        return None
    path = Path(directory) / f"{prompt.sha256[:16]}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(prompt.text, encoding="utf-8")
    return path
