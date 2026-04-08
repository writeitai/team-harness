from datetime import datetime
from datetime import timezone

OUTPUT_INSTRUCTION = """
---
IMPORTANT — output requirements:
1. Write AGENT_SUMMARY.md in your working directory when you finish. Include:
   - What you did
   - Which files you created or modified (with paths)
   - Any errors or blockers you encountered
   - Your final result or conclusion
2. Append one-line progress checkpoints to AGENT_PROGRESS.md as you work.
   Format each line as: [HH:MM:SS UTC] <what you just completed>
3. Write any substantial output (generated code, reports, analysis) to named
   files rather than printing them to stdout.
---
""".strip()

BASE_PROMPT = """
You are a coordinator agent. You orchestrate worker agents via tools.

Your job:
1. Understand the task and plan which agents to spawn.
2. Spawn agents with precise, self-contained prompts.
3. Monitor progress using read_new_agent_output and read_new_file_content for AGENT_PROGRESS.md.
4. Kill and respawn agents that appear stalled or have wrong output.
5. Read AGENT_SUMMARY.md from each agent's working directory for structured results.
6. Synthesise results and report back.

Mid-run feedback pattern for long tasks:
- Spawn all agents first.
- Call wait_for_any(agent_ids, timeout=60) — check in at most every 60 seconds.
- On each wakeup, call read_new_agent_output and read_new_file_content(cwd + "/AGENT_PROGRESS.md")
  for still-running agents.
- If an agent looks stalled, kill_agent and respawn with better instructions.
- Repeat until all done.

Use the todo tools to maintain a persistent task list at the start of each run and
after each significant step. This helps you stay coherent across many turns.

Always use absolute paths when reading files from agent working directories.
Use the cwd you passed to spawn_agent as the base for constructing absolute paths.
Always use tools to read files and check command output — never guess.
""".strip()


def build_system_prompt(config: object, allowed_types: list[str], skills: list) -> str:
    parts = [BASE_PROMPT]

    extension = getattr(config, "system_prompt_extension", "")
    if extension:
        parts.append(extension)

    parts.append(
        "\n".join(
            [
                f"Available agent types for this run: {', '.join(allowed_types)}",
                f"Working directory: {getattr(config, 'cwd', '.')}",
                f"Current UTC time: {datetime.now(timezone.utc).isoformat()}",
            ]
        )
    )

    if skills:
        parts.append(
            "Additional tools (skills) available:\n"
            + "\n".join(f"- {skill.name}: {skill.description}" for skill in skills)
        )

    return "\n\n---\n\n".join(part for part in parts if part)
