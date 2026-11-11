import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import Budget, VideoAgent  # noqa: E402
from higgsfield_client import GenerationFailed  # noqa: E402


def tool_use(id_, name, **inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


def text(t):
    return NS(type="text", text=t)


class FakeLLM:
    """Opus: vòng 1 gọi tool (2 worker + 2 shot), vòng 2 kết thúc. Worker trả text cố định."""

    def __init__(self, brain_turns):
        self.brain_turns = list(brain_turns)
        self.worker_models = []
        self.messages = self

    def create(self, **kw):
        if kw["model"] == "claude-opus-5-5":
            content, stop = self.brain_turns.pop(0)
            return NS(content=content, stop_reason=stop)
        self.worker_models.append(kw["model"])
        return NS(content=[text('{"prompt": "a quiet tea tin at sunset"}')], stop_reason="end_turn")


class FakeHF:
    def __init__(self, cost="0.50", fail=()):
        self.cost, self.fail, self.generated = Decimal(cost), set(fail), []

    def estimate(self, model, params):
        return self.cost

    def generate(self, model, params, dest, stem, **kw):
        shot = stem.split("_a")[0]
        if shot in self.fail:
            raise GenerationFailed("nsfw", request_id="r", status="nsfw")
        self.generated.append(stem)
        f = Path(dest) / f"{stem}.mp4"
        f.write_bytes(b"x")
        return {"files": [str(f)], "expires_at": "2026-11-16T00:00:00Z"}


def make_agent(tmp_path, llm, hf, budget="5", ask=lambda _: "y", **kw):
    return VideoAgent(llm, hf, Budget(Decimal(budget)), tmp_path, ask=ask,
                      out=lambda *_: None, **kw)


def test_full_loop_parallel_tools_and_result_order(tmp_path):
    llm = FakeLLM([
        ([tool_use("a", "delegate_task", tier="haiku", task="t", context="c", output_format="json"),
          tool_use("b", "delegate_task", tier="sonnet", task="t", context="c", output_format="json"),
          tool_use("c", "generate_video", shot_id="s01", prompt="p"),
          tool_use("d", "generate_video", shot_id="s02", prompt="p")], "tool_use"),
        ([text("Xong 2 shot.")], "end_turn"),
    ])
    hf = FakeHF()
    agent = make_agent(tmp_path, llm, hf)
    assert agent.run("goal") == "Xong 2 shot."
    assert sorted(llm.worker_models) == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert sorted(hf.generated) == ["s01_a1", "s02_a1"]
    assert agent.budget.spent == Decimal("1.00")


def test_dispatch_returns_results_in_block_order_and_unknown_tool(tmp_path):
    agent = make_agent(tmp_path, FakeLLM([]), FakeHF())
    blocks = [tool_use("1", "generate_video", shot_id="s01", prompt="p"),
              tool_use("2", "nope"),
              tool_use("3", "estimate_cost", prompt="p")]
    res = agent.dispatch(blocks)
    assert [r["tool_use_id"] for r in res] == ["1", "2", "3"]
    assert res[1]["is_error"] and "không tồn tại" in res[1]["content"]


def test_budget_blocks_overspend_and_attempt_cap(tmp_path):
    agent = make_agent(tmp_path, FakeLLM([]), FakeHF(cost="3.00"), budget="5")
    res = agent.dispatch([tool_use("1", "generate_video", shot_id="s01", prompt="p"),
                          tool_use("2", "generate_video", shot_id="s02", prompt="p")])
    assert "is_error" not in res[0]
    assert res[1]["is_error"] and "Vượt ngân sách" in res[1]["content"]

    agent2 = make_agent(tmp_path, FakeLLM([]), FakeHF(cost="0.10"), budget="5")
    for _ in range(2):
        agent2.dispatch([tool_use("x", "generate_video", shot_id="s01", prompt="p")])
    third = agent2.dispatch([tool_use("y", "generate_video", shot_id="s01", prompt="p")])
    assert third[0]["is_error"] and "lần thử" in third[0]["content"]


def test_user_decline_refunds_and_skips_render(tmp_path):
    hf = FakeHF()
    agent = make_agent(tmp_path, FakeLLM([]), hf, ask=lambda _: "n")
    res = agent.dispatch([tool_use("1", "generate_video", shot_id="s01", prompt="p")])
    assert res[0]["is_error"] and hf.generated == []
    assert agent.budget.spent == 0 and agent.budget.attempts["s01"] == 0


def test_failed_generation_is_refunded_but_attempt_is_kept(tmp_path):
    agent = make_agent(tmp_path, FakeLLM([]), FakeHF(fail={"s01"}))
    res = agent.dispatch([tool_use("1", "generate_video", shot_id="s01", prompt="p")])
    assert res[0]["is_error"] and "GenerationFailed" in res[0]["content"]
    assert agent.budget.spent == 0 and agent.budget.attempts["s01"] == 1


def test_review_rejects_path_traversal(tmp_path):
    (tmp_path.parent / "secret.mp4").write_bytes(b"x")
    agent = make_agent(tmp_path, FakeLLM([]), FakeHF())
    res = agent.dispatch([tool_use("1", "review_shot", file="../secret.mp4")])
    assert res[0]["is_error"]
