"""
Claude CLI client — calls the local `claude` binary via subprocess.
No cloud SDK, no API key required. Fully local-first.

Prompt is delivered via stdin (not a CLI argument) to avoid length limits
and shell-escaping issues with special characters.
"""

import subprocess

CLAUDE_TIMEOUT_SECONDS = 120


def ask_claude(prompt: str) -> str:
    """
    Send a prompt to the local Claude CLI via stdin and return the response.

    Requires the `claude` binary to be installed and available on PATH.
    Install via: https://docs.anthropic.com/en/docs/claude-cli

    All error conditions are normalised to RuntimeError so callers only need
    to catch one exception type.

    Raises:
        RuntimeError: on missing binary, non-zero exit code, or timeout.
    """
    try:
        result = subprocess.run(
            ["claude", "-p"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "Claude CLI not found. Install it and ensure it is on your PATH."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Claude CLI did not respond within {CLAUDE_TIMEOUT_SECONDS}s."
        )

    if result.returncode != 0:
        error_detail = result.stderr.strip() or "no stderr output"
        raise RuntimeError(
            f"Claude CLI exited with code {result.returncode}: {error_detail}"
        )

    return result.stdout.strip()


# Backward-compatible alias used by main.py
analyze = ask_claude

