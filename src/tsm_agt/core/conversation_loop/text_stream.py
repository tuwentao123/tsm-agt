"""Live response text is provisional until the existing turn gates accept it.

Fragments are not transcript commits. Their stable identity lets a UI replace
or retract a candidate after a protocol correction, review, steer, or error.
"""

from uuid import uuid4

from tsm_agt.core.agent_loop import AgentProgress, AgentProgressKind
from tsm_agt.ports import ModelTextDelta


class ResponseTextStream:
    def __init__(self, kernel, state, *, goal):
        self.kernel = kernel
        self.state = state
        self.goal = goal
        self.text_id = f"text-{uuid4().hex}"
        self.parts = []
        self.sequence = 0
        self.phase = "pending"
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.parts and not self.committed:
            self.status("interrupted" if exc_type else "discarded")

    def _emit(self, kind, *, text="", status="generating"):
        self.kernel._notify_agent_progress(self.state.on_progress, AgentProgress(
            kind, turn_id=self.state.turn_id,
            model_call=self.state.model_call_number,
            max_model_calls=self.state.checkpoint.max_model_calls,
            tool_call=self.state.tool_call_count,
            max_tool_calls=self.state.checkpoint.max_tool_calls,
            goal=self.goal,
            text_id=self.text_id, text=text,
            text_sequence=self.sequence, text_status=status, phase=self.phase,
        ))

    def delta(self, event: ModelTextDelta):
        if event.phase is not None:
            self.phase = event.phase.value
        self.parts.append(event.text)
        self.sequence += 1
        self._emit(AgentProgressKind.TEXT_DELTA, text=event.text)
        if self.state.on_text_delta is not None:
            self.state.on_text_delta(event.text)

    def resolve(self, response, *, has_tools):
        self.phase = "commentary" if has_tools else "final_answer"
        if not self.parts and response.message.text:
            # Non-streaming providers still use the same display lifecycle.
            self.delta(ModelTextDelta(response.message.text))
        self.status("generating" if has_tools else "reviewing")

    def status(self, status):
        if self.parts:
            self._emit(AgentProgressKind.TEXT_STATE, status=status)

    def commit(self):
        self.status("committed")
        self.committed = True
