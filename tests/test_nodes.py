"""A model per node (issue #48): the roster, its overrides, and what each node records."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from foood.app import create_app
from foood.nodes import NODES, chat_for, client_for, load, model_for, timeout_for
from tests.support import login
from tests.test_graph_engine import QA_OWNER, _paid

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("FOOOD_MODELS", "FOOOD_MODEL_ALL", "FOOOD_LLM", "FOOOD_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    for node in NODES:
        monkeypatch.delenv(f"FOOOD_MODEL_{node.upper()}", raising=False)


def test_the_committed_roster_names_every_node() -> None:
    roster = load(ROOT / "models.toml")
    assert set(roster.models) == set(NODES)
    assert roster.models["decide"] == "gpt-5.4-mini" and roster.models["escalate"] == "gpt-5.4"
    assert roster.models["phrase"] == "gpt-5.4-nano" and roster.models["lookup"] == "gpt-5.4-nano"
    assert roster.models["suggest"] == "gpt-5.4-mini" and roster.models["intake"] == "gpt-5.4-mini"
    assert roster.timeout_for("decide") == 20 and roster.timeout_for("classify") == 10
    assert roster.to_json()["models"]["escalate"] == "gpt-5.4"


def test_the_environment_overrides_one_node_or_all(monkeypatch, tmp_path: Path) -> None:
    file = tmp_path / "models.toml"
    file.write_text(
        '[nodes]\ndecide = "a"\nphrase = "b"\n[timeouts]\ndecide = 7\n', encoding="utf-8"
    )
    monkeypatch.setenv("FOOOD_MODELS", str(file))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert model_for("decide") == "a" and model_for("phrase") == "b" and model_for("lookup") is None
    assert timeout_for("decide") == 7 and timeout_for("phrase") == 20
    monkeypatch.setenv("FOOOD_MODEL_PHRASE", "off")
    assert model_for("phrase") is None
    monkeypatch.setenv("FOOOD_MODEL_ALL", "tiny")
    assert all(model_for(node) == "tiny" for node in NODES if node != "phrase")
    assert model_for("phrase") is None  # a single node's override wins over ALL


def test_a_bad_node_name_is_refused(tmp_path: Path) -> None:
    file = tmp_path / "models.toml"
    file.write_text('[nodes]\nguess = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load(file)
    with pytest.raises(ValueError):
        load(ROOT / "models.toml").model_for("guess")


def test_no_key_or_off_means_no_model_anywhere(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert (
        model_for("decide") is None and client_for("phrase") is None and chat_for("decide") is None
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("FOOOD_LLM", "off")
    assert model_for("decide") is None


def test_clients_carry_the_nodes_model_and_timeout(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("FOOOD_MODEL_PHRASE", "gpt-5.4-nano")
    client = client_for("phrase")
    assert client is not None and client.name == "gpt-5.4-nano"
    chat = chat_for("decide")
    assert chat is not None and chat.model_name == "gpt-5.4-mini" and chat.max_retries == 0


def test_the_nodes_models_feed_the_advisor_intake_and_phraser(monkeypatch) -> None:
    from foood.intake import OpenAIIntake
    from foood.messages import OpenAIPhraser
    from foood.recovery import OpenAIAdvisor

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("FOOOD_MODEL_DECIDE", "d-model")
    monkeypatch.setenv("FOOOD_MODEL_INTAKE", "i-model")
    monkeypatch.setenv("FOOOD_MODEL_PHRASE", "p-model")
    assert OpenAIAdvisor().model == "d-model"
    assert OpenAIIntake().model == "i-model"
    assert OpenAIPhraser().model == "p-model"
    assert OpenAIAdvisor(model="other").model == "other"


@asynccontextmanager
async def _app(engine, phraser=None) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(simulate=False, engine=engine, phraser=phraser)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.store = app.state.store  # type: ignore[attr-defined]
        yield c


async def _order(client) -> dict:
    sara = await login(client, "demo.customer@example.com")
    return await _paid(client, sara)


async def _reject(client, order: dict) -> None:
    owner = await login(client, QA_OWNER)
    rejected = await client.patch(
        f"/api/orders/{order['id']}/status",
        json={"status": "CANCELLED", "reason": "The grill is down"},
        headers=owner,
    )
    assert rejected.status_code == 200, rejected.text


async def test_every_call_names_its_node_and_the_bench_sums_per_node(tmp_path: Path) -> None:
    from foood.bench import by_node
    from foood.graph_engine import LangGraphEngine
    from foood.messages import ScriptedPhraser
    from foood.recovery import Recovery, ScriptedAdvisor

    advisor = ScriptedAdvisor(Recovery(tool="replace_order", reasoning="QA Bistro can"))
    engine = LangGraphEngine(advisor=advisor)
    async with _app(engine, phraser=ScriptedPhraser("Your food is coming.")) as client:
        order = await _order(client)
        await _reject(client, order)
        decisions = client.store.decisions[order["id"]]
        nodes = {d.get("node") for d in decisions if d.get("llmCalls")}
        assert nodes == {"decide", "phrase"}
        per_node = by_node(decisions)
        assert per_node["decide"]["calls"] == 1 and per_node["phrase"]["calls"] >= 1
        assert set(per_node["decide"]) == {"calls", "tokens", "cost", "latencyMs", "models"}
        assert per_node["decide"]["models"] == ["scripted"]


async def test_escalation_is_one_frontier_call_when_decide_fails() -> None:
    from foood.graph_engine import LangGraphEngine
    from foood.recovery import Recovery, ScriptedAdvisor

    decide = ScriptedAdvisor(Recovery(tool="issue_credit", reasoning="nope"))  # not allowed here
    escalate = ScriptedAdvisor(Recovery(tool="replace_order", reasoning="the frontier agrees"))
    engine = LangGraphEngine(advisor=decide, escalator=escalate)
    async with _app(engine) as client:
        order = await _order(client)
        await _reject(client, order)
        names = [d["decision"] for d in client.store.decisions[order["id"]]]
        assert "escalate: replace_order" in names and "fallback" not in names
        recorded = next(
            d for d in client.store.decisions[order["id"]] if d["decision"].startswith("escalate:")
        )
        assert recorded["node"] == "escalate" and recorded["llmRole"] == "decision"
        assert len(escalate.contexts) == 1 and len(decide.contexts) == 1


async def test_without_an_escalator_a_bad_pick_falls_back_as_before() -> None:
    from foood.graph_engine import LangGraphEngine
    from foood.recovery import Recovery, ScriptedAdvisor

    engine = LangGraphEngine(advisor=ScriptedAdvisor(Recovery(tool="issue_credit", reasoning="x")))
    async with _app(engine) as client:
        order = await _order(client)
        await _reject(client, order)
        names = [d["decision"] for d in client.store.decisions[order["id"]]]
        assert "fallback" in names and not any(n.startswith("escalate:") for n in names)
