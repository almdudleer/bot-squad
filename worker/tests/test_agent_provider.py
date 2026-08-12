from __future__ import annotations

from bot_squad_worker import agent_provider


def test_codex_fresh_and_resume_commands_use_unattended_cli():
    provider = agent_provider.get("codex")
    fresh = provider.launch_command(initial_prompt="read the brief")
    resumed = provider.launch_command(
        resume_id="019fb6c7-5c49-7120-bc84-74854cfdfe1a",
        model="gpt-5.6",
    )

    assert fresh == (
        "codex --dangerously-bypass-approvals-and-sandbox"
    )
    assert resumed.startswith(
        "codex resume --dangerously-bypass-approvals-and-sandbox "
        "--model gpt-5.6 019fb6c7-5c49-7120-bc84-74854cfdfe1a"
    )


def test_provider_choice_keeps_project_default_unless_provider_or_codex_model_is_explicit():
    assert agent_provider.provider_for_model("", "claude") == "claude"
    assert agent_provider.provider_for_model("", "codex") == "codex"
    assert agent_provider.provider_for_model("codex", "claude") == "codex"
    assert agent_provider.provider_for_model("opus", "codex") == "codex"
    assert agent_provider.provider_for_model("terra", "claude") == "codex"
    assert agent_provider.provider_for_model(
        "sonnet", "codex", explicit_provider="claude"
    ) == "claude"
