"""LLM backends. The agent only talks to the small Session interface below, so the
Gemini code stays in one place and the tests can run with a scripted fake model."""
import time
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    name: str
    args: dict
    id: str = None


@dataclass
class Turn:
    calls: list                 # list[ToolCall]; empty means the model sent no tool call
    text: str = ""
    in_tokens: int = 0
    out_tokens: int = 0         # includes thinking tokens, which are billed as output
    seconds: float = 0.0        # time inside the API call only


class QuotaExhausted(Exception):
    """Daily free-tier quota is used up. The eval run stops and can resume later."""


class FatalAPIError(Exception):
    """Bad key, billing problem, or unknown model. Retrying or falling back would only
    hide it, so the eval run stops."""


# Paid-tier list prices, USD per 1M tokens, from ai.google.dev/gemini-api/docs/pricing
# (checked 2026-10-24). The free tier costs $0; these let us report what a run *would* cost.
PRICES = {
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
}


def cost_usd(model: str, in_tokens: int, out_tokens: int):
    if model not in PRICES:
        return None
    p_in, p_out = PRICES[model]
    return (in_tokens * p_in + out_tokens * p_out) / 1e6


# ----------------------------------------------------------------------------- Gemini
class GeminiBackend:
    def __init__(self, model: str, api_key: str, rpm: float = 8, thinking_budget=None):
        from google import genai
        self.genai = genai
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.min_interval = 60.0 / rpm if rpm else 0.0
        self.thinking_budget = thinking_budget
        self._last = 0.0
        self.waited = 0.0   # seconds spent on rate limits and retries (not counted as latency)

    def start(self, system: str, user: str, tools: list) -> "GeminiSession":
        return GeminiSession(self, system, user, tools)

    def _throttle(self):
        gap = time.monotonic() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
            self.waited += self.min_interval - gap
        self._last = time.monotonic()

    def generate(self, contents, config):
        from google.genai import errors
        delay = 10.0
        for attempt in range(6):
            self._throttle()
            t = time.perf_counter()
            try:
                resp = self.client.models.generate_content(model=self.model, contents=contents, config=config)
                return resp, time.perf_counter() - t
            except errors.APIError as e:
                msg = str(e)
                if e.code == 429 and ("PerDay" in msg or "per day" in msg.lower()):
                    raise QuotaExhausted(msg[:300])
                if e.code in (400, 401, 402, 403, 404):
                    raise FatalAPIError(f"{e.code}: {msg[:400]}")
                if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                    time.sleep(delay)
                    self.waited += delay
                    delay = min(delay * 2, 90)
                    continue
                raise


class GeminiSession:
    def __init__(self, backend: GeminiBackend, system: str, user: str, tools: list):
        from google.genai import types
        self.types = types
        self.backend = backend
        self.contents = [types.Content(role="user", parts=[types.Part.from_text(text=user)])]
        self.system = system
        self.decls = [types.FunctionDeclaration(name=t["name"], description=t["description"],
                                                parameters_json_schema=t["parameters"]) for t in tools]

    def _config(self, force_submit: bool):
        t = self.types
        kwargs = dict(
            system_instruction=self.system,
            tools=[t.Tool(function_declarations=self.decls)],
            tool_config=t.ToolConfig(function_calling_config=t.FunctionCallingConfig(
                mode="ANY", allowed_function_names=["submit_report"] if force_submit else None)),
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
        )
        if self.backend.thinking_budget is not None:
            kwargs["thinking_config"] = t.ThinkingConfig(thinking_budget=self.backend.thinking_budget)
        return t.GenerateContentConfig(**kwargs)

    def step(self, tool_results=None, force_submit=False, nudge=None) -> Turn:
        t = self.types
        if tool_results:
            self.contents.append(t.Content(role="user", parts=[
                t.Part(function_response=t.FunctionResponse(id=call.id, name=call.name, response={"result": out}))
                for call, out in tool_results]))
        if nudge:
            self.contents.append(t.Content(role="user", parts=[t.Part.from_text(text=nudge)]))
        resp, secs = self.backend.generate(self.contents, self._config(force_submit))
        usage = resp.usage_metadata
        in_tok = (usage.prompt_token_count or 0) if usage else 0
        out_tok = ((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)) if usage else 0
        cand = resp.candidates[0] if resp.candidates else None
        if cand is not None and cand.content is not None:
            self.contents.append(cand.content)   # keeps thought signatures intact
        calls = [ToolCall(fc.name, dict(fc.args or {}), getattr(fc, "id", None)) for fc in (resp.function_calls or [])]
        text = ""
        if not calls and cand is not None and cand.content is not None:
            text = "".join(p.text or "" for p in cand.content.parts or [] if not getattr(p, "thought", False))
        return Turn(calls=calls, text=text, in_tokens=in_tok, out_tokens=out_tok, seconds=secs)


# ----------------------------------------------------------------------------- fake
@dataclass
class FakeBackend:
    """Replays a scripted list of turns per session. Used by the unit tests."""
    script: list = field(default_factory=list)   # list of lists of ToolCall (or [] for "no call")
    model: str = "fake"
    waited: float = 0.0

    def start(self, system, user, tools):
        return FakeSession(list(self.script))


class FakeSession:
    def __init__(self, script):
        self.script = script
        self.received = []

    def step(self, tool_results=None, force_submit=False, nudge=None) -> Turn:
        self.received.append((tool_results, force_submit, nudge))
        calls = self.script.pop(0) if self.script else []
        return Turn(calls=calls, in_tokens=100, out_tokens=20, seconds=0.01)
