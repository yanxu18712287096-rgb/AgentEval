"""Core agent loop.

This is the heart of CoreCoder.  The pattern is simple:

    user message -> LLM (with tools) -> tool calls? -> execute -> loop
                                      -> text reply? -> return to user

It keeps looping until the LLM responds with plain text (no tool calls),
which means it's done working and ready to report back.
"""

import concurrent.futures
import inspect
import time
import uuid

from .context import ContextManager
from .llm import LLM
from .permissions import Permission
from .prompt import PLAN_MODE_PROMPT, system_prompt
from .tools import ALL_TOOLS
from .tools.agent import AgentTool
from .tools.base import Tool
from .tools.todo import TodoWriteTool


class Agent:
    def __init__(
        self,
        llm: LLM,
        tools: list[Tool] | None = None,
        max_context_tokens: int = 128_000,
        max_rounds: int = 50,
        permission=None,
        hooks=None,
        system_prompt_override: str | None = None,
        trace=None,
    ):
        self.llm = llm
        self.tools = tools if tools is not None else ALL_TOOLS
        self.permission = permission
        self.hooks = hooks
        self._tool_by_name = {t.name: t for t in self.tools}
        self.messages: list[dict] = []
        self.context = ContextManager(max_tokens=max_context_tokens)
        self.max_rounds = max_rounds
        self._system = system_prompt(self.tools) if system_prompt_override is None else system_prompt_override
        self.trace = trace
        self._agent_id = uuid.uuid4().hex
        self._turn = 0
        self._round = 0
        self._pending_trace = {}
        self.plan_mode = False  # toggled by /plan; while on, mutating tools are refused

        # wire up sub-agent capability
        for t in self.tools:
            if isinstance(t, AgentTool):
                t._parent_agent = self

        self._todo = next((t for t in self.tools if isinstance(t, TodoWriteTool)), None)

    def _full_messages(self) -> list[dict]:
        system = self._system
        # re-injected every round, like the task list below, so a toggle made
        # between turns takes effect on the very next request
        if self.plan_mode:
            system += "\n\n" + PLAN_MODE_PROMPT
        # the task list is re-injected every round, so the model always sees the
        # current state rather than a stale copy buried in old tool results
        if self._todo is not None:
            rendered = self._todo.render()
            if rendered:
                system += "\n\n# Current task list\n" + rendered
        return [{"role": "system", "content": system}] + self.messages

    def _tool_schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools]

    def _compress_context(self):
        changed = self.context.maybe_compress(self.messages, self.llm)
        if self.context.last_event is not None:
            self._record("context_compression", **self.context.last_event)
        return changed

    def chat(self, user_input: str, on_token=None, on_tool=None, on_reasoning=None) -> str:
        self._turn += 1
        self._record("user_input", content=user_input)
        try:
            result = self._chat(user_input, on_token, on_tool, on_reasoning)
            self._record("turn_finished", content=result,
                         status="limit_reached" if result == "(reached maximum tool-call rounds)" else "completed")
            return result
        except BaseException as exc:
            for tc in list(self._pending_trace.values()):
                self._finish_trace(tc["call"], "interrupted" if isinstance(exc, KeyboardInterrupt) else "aborted",
                                   "Execution did not return a recorded result; side effects may have occurred.")
            self._record("turn_failed", error_type=type(exc).__name__)
            raise

    def _record(self, event, **data):
        if self.trace is not None:
            self.trace.record(event, agent_id=self._agent_id, turn=self._turn, round=self._round, **data)

    def _finish_trace(self, tc, status, result, elapsed_seconds=None):
        pending = self._pending_trace.pop(id(tc), None)
        if pending is not None:
            self._record("tool_finished", call_key=pending["key"], tool_call_id=tc.id,
                         tool_name=tc.name, status=status, result=result,
                         elapsed_seconds=elapsed_seconds)

    def _gate(self, tc):
        result = self._pre_hooks(tc) or self._permit(tc)
        if result is not None:
            self._finish_trace(tc, "blocked", result)
        return result

    def _chat(self, user_input: str, on_token=None, on_tool=None, on_reasoning=None) -> str:
        """Process one user message. May involve multiple LLM/tool rounds."""
        self.messages.append({"role": "user", "content": user_input})
        self._compress_context()

        for round_index in range(self.max_rounds):
            self._round = round_index + 1
            self._record("model_requested", messages=self._full_messages(), tools=self._tool_schemas())
            model_started = time.monotonic()
            resp = self.llm.chat(
                messages=self._full_messages(),
                tools=self._tool_schemas(),
                on_token=on_token,
                on_reasoning=on_reasoning,
            )
            self._record("model_response", message=resp.message, prompt_tokens=resp.prompt_tokens,
                         completion_tokens=resp.completion_tokens,
                         usage_available=resp.usage_available,
                         elapsed_seconds=time.monotonic() - model_started)
            for index, tc in enumerate(resp.tool_calls):
                key = f"{self._agent_id}:{self._turn}:{self._round}:{index}"
                self._pending_trace[id(tc)] = {"key": key, "call": tc}
                self._record("tool_requested", call_key=key, tool_call_id=tc.id,
                             tool_name=tc.name, arguments=tc.arguments)

            # no tool calls -> LLM is done, return text
            if not resp.tool_calls:
                self.messages.append(resp.message)
                return resp.content

            # tool calls -> execute (parallel when multiple, like Claude Code's
            # StreamingToolExecutor which runs independent tools concurrently)
            self.messages.append(resp.message)

            try:
                if len(resp.tool_calls) == 1:
                    tc = resp.tool_calls[0]
                    if on_tool:
                        on_tool(tc.name, tc.arguments)
                    result = self._gate(tc)
                    if result is None:
                        result = self._exec_tool(tc)
                        self._post_hooks(tc, result)
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
                else:
                    # parallel execution for multiple tool calls
                    results = self._exec_tools_parallel(resp.tool_calls, on_tool)
                    for tc, result in zip(resp.tool_calls, results):
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result,
                        })
            except KeyboardInterrupt:
                # Ctrl+C mid-execution would leave the assistant tool_calls
                # message without replies, poisoning the next request; backfill
                self._answer_pending_tool_calls(resp.tool_calls)
                raise

            # compress if tool outputs are big
            self._compress_context()

        return "(reached maximum tool-call rounds)"

    def _pre_hooks(self, tc) -> str | None:
        """PreToolUse hooks, fired before consent. A string return blocks the
        call and becomes the tool result the model sees; None lets it through."""
        if self.hooks is None:
            return None
        return self.hooks.run_pre(tc.name, tc.arguments)

    def _post_hooks(self, tc, result: str):
        """PostToolUse hooks observe a finished call; they can never block."""
        if self.hooks is not None:
            self.hooks.run_post(tc.name, tc.arguments, result)

    def _permit(self, tc) -> str | None:
        """Consent check for one call. None means go ahead; a string is the
        refusal, returned as the tool result instead of executing."""
        # plan mode outranks consent, even --yes: while it's on nothing mutates
        if self.plan_mode and tc.name not in Permission.READ_ONLY:
            return (
                "Plan mode is on, so this call was refused: plan mode is "
                "read-only. Do not retry it. Keep investigating with the "
                "read-only tools, then present the plan and stop. The user can "
                'approve it by typing "approve", or exit plan mode with /plan.'
            )
        if self.permission is None:
            return None
        return self.permission.check(tc.name, tc.arguments)

    def _exec_tool(self, tc) -> str:
        started = time.monotonic()
        pending = self._pending_trace.get(id(tc))
        if pending:
            self._record("tool_started", call_key=pending["key"], tool_call_id=tc.id, tool_name=tc.name)
        result = self._execute_tool(tc)
        self._finish_trace(tc, "failed" if result.startswith(("Error:", "Error executing ")) else "returned", result,
                           elapsed_seconds=time.monotonic() - started)
        return result

    def _execute_tool(self, tc) -> str:
        """Execute a single tool call, returning the result string."""
        tool = self._tool_by_name.get(tc.name)
        if tool is None:
            return f"Error: unknown tool '{tc.name}'"
        # validate arguments first so a TypeError raised *inside* the tool isn't
        # mislabelled as a bad-arguments error from the caller
        try:
            inspect.signature(tool.execute).bind(**tc.arguments)
        except TypeError as e:
            return f"Error: bad arguments for {tc.name}: {e}"
        # a tool that blows up gets reported back as text, never kills the loop
        try:
            return tool.execute(**tc.arguments)
        except Exception as e:  # noqa: BLE001
            return f"Error executing {tc.name}: {e}"

    def _exec_tools_parallel(self, tool_calls, on_tool=None) -> list[str]:
        """Run multiple tool calls concurrently using threads.

        This is inspired by Claude Code's StreamingToolExecutor which starts
        executing tools while the model is still generating.  We simplify to:
        when the model returns N tool calls at once, run them in parallel.
        """
        from .tools.bash import get_tracked_cwd, set_tracked_cwd

        for tc in tool_calls:
            if on_tool:
                on_tool(tc.name, tc.arguments)

        # hooks and consent are settled up front on this thread: prompting
        # from pool workers would interleave several prompts on one terminal
        results = [self._gate(tc) for tc in tool_calls]
        # the tracked cwd is thread-local, so pool workers would otherwise
        # start from the launch directory: hand the session cwd in, and merge
        # any cd back in call order so the batch behaves like a sequence
        session_cwd = get_tracked_cwd()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            def run(i, tc, base_cwd):
                is_bash = tc.name == "bash"
                if is_bash and base_cwd is not None:
                    set_tracked_cwd(base_cwd)
                out = self._exec_tool(tc)
                if not is_bash:
                    return i, out, None
                cwd_after = get_tracked_cwd()
                # only a worker that actually moved the dir reports back; a
                # sibling that merely inherited the base must not revert a cd
                return i, out, cwd_after if cwd_after != base_cwd else None

            futures = {
                i: pool.submit(run, i, tc, session_cwd)
                for i, tc in enumerate(tool_calls)
                if results[i] is None
            }
            for i, future in futures.items():
                i, results[i], worker_cwd = future.result()
                if worker_cwd is not None:
                    session_cwd = worker_cwd
                    set_tracked_cwd(worker_cwd)
        for i in futures:
            self._post_hooks(tool_calls[i], results[i])
        return results

    def _answer_pending_tool_calls(self, tool_calls):
        """Backfill a tool reply for every call that didn't get one.

        OpenAI-compatible APIs reject a request where an assistant message has
        tool_calls without a matching tool reply for each id, so this keeps the
        history valid when execution is interrupted partway through.
        """
        answered = {m.get("tool_call_id") for m in self.messages if m.get("role") == "tool"}
        for tc in tool_calls:
            if tc.id not in answered:
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": "[interrupted]",
                })

    def reset(self):
        """Clear conversation history."""
        self.messages.clear()
        # a user-visible reset must not leak the dead conversation's checklist
        # into the next system prompt
        todo = self._tool_by_name.get("todo_write")
        if isinstance(todo, TodoWriteTool):
            todo._tasks = []
