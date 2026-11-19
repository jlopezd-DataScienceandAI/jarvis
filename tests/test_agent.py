"""The conversation loop, driven by a scripted stand-in for the Anthropic client."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from jarvis.brain.agent import Agent
from jarvis.brain.tools import ToolRegistry


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_block(name, tool_input, block_id="toolu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=block_id)


def thinking_block():
    return SimpleNamespace(type="thinking", thinking="", signature="sig")


def message(content, stop_reason="end_turn", cache_read=0):
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=cache_read,
                            cache_creation_input_tokens=0)
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage)


class FakeStream:
    def __init__(self, final, on_event=None):
        self._final = final
        self._on_event = on_event

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for block in self._final.content:
            if block.type == "text":
                for word in block.text.split(" "):
                    if self._on_event:
                        self._on_event()
                    yield SimpleNamespace(type="text", text=word + " ")
            elif block.type == "raw_text":  # one delta, exactly as written (no trailing space)
                yield SimpleNamespace(type="text", text=block.text)
                yield SimpleNamespace(type="content_block_stop", content_block=SimpleNamespace(type="text"))
            else:
                yield SimpleNamespace(type="content_block_stop", content_block=block)

    def get_final_message(self):
        return self._final


class FakeClient:
    """Replays scripted responses and records every request it was sent."""

    def __init__(self, *responses, on_event=None):
        self._responses = list(responses)
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(stream=self._stream)
        self._on_event = on_event

    def _stream(self, **kwargs):
        # Snapshot: the agent keeps mutating its own list after the call.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return FakeStream(response, self._on_event)


@pytest.fixture
def tools():
    local = ToolRegistry()
    local.calls = []  # type: ignore[attr-defined]

    class EchoArgs(BaseModel):
        value: str

    @local.tool("echo", "test", EchoArgs)
    def _echo(args, ctx):
        local.calls.append(args.value)
        return f"eco: {args.value}"

    return local


def make_agent(config, ctx, tools, client):
    config.brain.web_search = False
    return Agent(config, ctx, client=client, registry=tools)


def test_plain_reply_streams_and_is_stored(config, ctx, tools):
    client = FakeClient(message([thinking_block(), text_block("A su servicio, señor.")]))
    agent = make_agent(config, ctx, tools, client)
    heard = []
    reply = agent.respond("hola", heard.append)

    assert reply.strip() == "A su servicio, señor."
    assert "".join(heard) == reply
    request = client.requests[0]
    assert request["model"] == "claude-haiku-5-5"
    assert request["output_config"] == {"effort": "low"}
    assert "temperature" not in request and "thinking" not in request
    # The timestamp travels in the user turn so the system prompt stays cacheable.
    assert request["messages"][0]["content"].endswith("\nhola")
    assert request["messages"][0]["content"].startswith("[")
    assert "20" not in request["system"].split("## Carpetas")[0]  # no date baked into the prompt
    # The full content, thinking block included, is what goes back into the history.
    assert [b.type for b in agent.messages[1]["content"]] == ["thinking", "text"]


def test_tool_round_trip_keeps_results_in_one_user_message(config, ctx, tools):
    client = FakeClient(
        message([text_block("Enseguida."), tool_block("echo", {"value": "a"}, "t1"),
                 tool_block("echo", {"value": "b"}, "t2")], stop_reason="tool_use"),
        message([text_block("Hecho.")]),
    )
    agent = make_agent(config, ctx, tools, client)
    reply = agent.respond("haz eco")

    assert tools.calls == ["a", "b"]
    assert reply.split() == ["Enseguida.", "Hecho."]
    roles = [m["role"] for m in agent.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    results = agent.messages[2]["content"]
    assert [(r["tool_use_id"], r["content"]) for r in results] == [("t1", "eco: a"), ("t2", "eco: b")]
    # Second request must carry the history unchanged plus the results (append-only).
    assert client.requests[1]["messages"][:1] == client.requests[0]["messages"]


def test_invalid_tool_input_is_reported_not_run(config, ctx, tools):
    client = FakeClient(
        message([tool_block("echo", {"wrong": 1})], stop_reason="tool_use"),
        message([text_block("Vaya.")]),
    )
    agent = make_agent(config, ctx, tools, client)
    agent.respond("x")
    assert tools.calls == []
    [result] = agent.messages[2]["content"]
    assert result["is_error"] is True


def test_truncated_tool_call_is_never_executed(config, ctx, tools):
    client = FakeClient(message([tool_block("echo", {"value": "parcial"})], stop_reason="max_tokens"))
    agent = make_agent(config, ctx, tools, client)
    reply = agent.respond("x")
    assert tools.calls == []
    assert "cortado" in reply
    assert [m["role"] for m in agent.messages] == ["user"]  # the broken turn is not stored


def test_refusal_resets_the_conversation(config, ctx, tools):
    client = FakeClient(message([], stop_reason="refusal"), message([text_block("Hola.")]))
    agent = make_agent(config, ctx, tools, client)
    assert "no puedo ayudarle" in agent.respond("algo")
    assert agent.messages == []
    agent.respond("hola")
    assert len(client.requests[1]["messages"]) == 1


def test_pause_turn_is_resumed(config, ctx, tools):
    paused = message([SimpleNamespace(type="server_tool_use", name="web_search", input={}, id="s1")],
                     stop_reason="pause_turn")
    client = FakeClient(paused, message([text_block("Encontrado.")]))
    agent = make_agent(config, ctx, tools, client)
    assert agent.respond("busca").strip() == "Encontrado."
    assert [m["role"] for m in agent.messages] == ["user", "assistant", "assistant"]


def test_interruption_mid_stream_discards_the_partial_turn(config, ctx, tools):
    cancel = threading.Event()
    seen = []

    def on_event():
        seen.append(1)
        if len(seen) == 3:
            cancel.set()

    client = FakeClient(
        message([text_block("uno dos tres cuatro cinco seis")]),
        message([text_block("Dígame.")]),
        on_event=on_event,
    )
    agent = make_agent(config, ctx, tools, client)
    reply = agent.respond("cuenta", cancel=cancel)

    assert reply.split() == ["uno", "dos"]
    assert [m["role"] for m in agent.messages] == ["user"]  # nothing half-finished in the history

    agent.respond("otra cosa")
    follow_up = client.requests[1]["messages"][-1]["content"]
    assert "interrumpió" in follow_up and follow_up.endswith("otra cosa")


def test_interruption_during_tools_still_returns_their_results(config, ctx, tools):
    cancel = threading.Event()
    local = tools

    class NoArgs(BaseModel):
        pass

    @local.tool("slow", "test", NoArgs)
    def _slow(args, ctx):
        cancel.set()  # the user interrupts while the tool is running
        return "terminado"

    client = FakeClient(message([tool_block("slow", {})], stop_reason="tool_use"))
    agent = make_agent(config, ctx, tools, client)
    agent.respond("x", cancel=cancel)
    # A tool_use must always be followed by its tool_result, or the next request is invalid.
    assert [m["role"] for m in agent.messages] == ["user", "assistant", "user"]
    assert agent.messages[2]["content"][0]["content"] == "terminado"
    assert len(client.requests) == 1


def test_api_errors_are_spoken_not_raised(config, ctx, tools):
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    client = FakeClient(anthropic.APIConnectionError(request=request))
    agent = make_agent(config, ctx, tools, client)
    assert "conexión" in agent.respond("hola")


def test_rejected_web_tools_are_dropped_and_the_turn_retried(config, ctx, tools):
    import anthropic
    import httpx2

    response = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    rejected = anthropic.BadRequestError("web_search is not enabled", response=response, body=None)
    client = FakeClient(rejected, message([text_block("Sin web, pero aquí estoy.")]))
    config.brain.web_search = True
    agent = Agent(config, ctx, client=client, registry=tools)
    assert any(t.get("name") == "web_search" for t in client_tools(agent))

    reply = agent.respond("hola")
    assert "aquí estoy" in reply
    assert all(t.get("name") != "web_search" for t in client.requests[1]["tools"])


def client_tools(agent):
    return agent.tools


def test_conversation_restarts_when_idle_or_too_large(config, ctx, tools):
    client = FakeClient(*(message([text_block("ok")]) for _ in range(3)))
    agent = make_agent(config, ctx, tools, client)
    agent.respond("uno")
    agent.respond("dos")
    assert len(client.requests[1]["messages"]) == 3

    agent.context_tokens = config.brain.max_context_tokens + 1
    agent.respond("tres")
    assert len(client.requests[2]["messages"]) == 1


def test_profile_is_loaded_into_the_system_prompt(config, ctx, tools):
    (config.memory_dir / "profile.md").write_text("# Perfil\n\n- Prefiere el café solo.\n", encoding="utf-8")
    agent = make_agent(config, ctx, tools, FakeClient())
    assert "Prefiere el café solo." in agent.system


def raw_text_block(text):
    return SimpleNamespace(type="raw_text", text=text)


def test_sentence_before_a_tool_call_is_released_and_not_fused_with_the_next(config, ctx, tools):
    from jarvis.text import SentenceChunker

    client = FakeClient(
        message([raw_text_block("Enseguida, señor."), tool_block("echo", {"value": "a"})], stop_reason="tool_use"),
        message([raw_text_block("He terminado.")]),
    )
    agent = make_agent(config, ctx, tools, client)
    chunker = SentenceChunker()
    spoken_before_tool: list[str] = []

    def on_text(delta):
        sentences = chunker.feed(delta)
        if not tools.calls:  # the tool has not run yet
            spoken_before_tool.extend(sentences)

    reply = agent.respond("hazlo", on_text)
    assert spoken_before_tool == ["Enseguida, señor."]  # said while the tool runs, not after
    assert reply == "Enseguida, señor. He terminado. "


def test_text_split_mid_sentence_by_citations_is_left_alone(config, ctx, tools):
    client = FakeClient(message([raw_text_block("Según la web, "), raw_text_block("cuesta cuatrocientos euros")]))
    agent = make_agent(config, ctx, tools, client)
    assert agent.respond("precio") == "Según la web, cuesta cuatrocientos euros"
