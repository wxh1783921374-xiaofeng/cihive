from __future__ import annotations

from types import SimpleNamespace

from agents import Runner

from cihive.model import OpenAIModelAdapter, create_model_adapter
from cihive.models import RepairPlan, RepairProposal
from tests.conftest import make_failure_context, make_failure_run, make_patch, make_plan


async def test_openai_adapter_builds_planning_and_repair_agents(monkeypatch, settings) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    settings.model_provider = "openai"
    adapter = create_model_adapter(settings)
    assert isinstance(adapter, OpenAIModelAdapter)

    async def fake_run(agent, input, max_turns):
        assert max_turns <= settings.max_model_turns
        if agent.output_type is RepairPlan:
            return SimpleNamespace(final_output=make_plan())
        assert len(agent.tools) == 3
        return SimpleNamespace(
            final_output=RepairProposal(
                summary="Fix arithmetic",
                root_cause="Wrong operator",
                unified_diff=make_patch(),
                confidence=0.9,
            )
        )

    monkeypatch.setattr(Runner, "run", fake_run)
    run = make_failure_run()
    failure = make_failure_context(run)
    plan = await adapter.plan_failure(failure)
    proposal = await adapter.generate_repair(
        failure,
        plan,
        {"mathx.py": "def add(a, b):\n    return a - b\n"},
        3,
    )
    assert plan.summary
    assert proposal.summary == "Fix arithmetic"
