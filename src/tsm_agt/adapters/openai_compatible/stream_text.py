"""Withhold internal conclusion tails and text-encoded tool protocols."""


class VisibleTextStream:
    def __init__(self):
        self.tail = ""
        self.conclusion = False
        self.tool_candidate = True
        self.pending = ""

    def feed(self, text: str) -> str:
        if self.conclusion:
            return ""
        self.tail += text
        marker = "<tsm-conclusion-v1>"
        position = self.tail.find(marker)
        if position >= 0:
            visible = self.tail[:position]
            self.tail = ""
            self.conclusion = True
        else:
            held = next((
                size for size in range(min(len(marker) - 1, len(self.tail)), 0, -1)
                if self.tail.endswith(marker[:size])
            ), 0)
            visible = self.tail[:-held] if held else self.tail
            self.tail = self.tail[-held:] if held else ""
        return self._release(visible)

    def _release(self, text):
        if not self.tool_candidate:
            return text
        self.pending += text
        candidate = self.pending.lstrip()
        self.tool_candidate = (
            "<tool_use".startswith(candidate) or candidate.startswith("<tool_use")
        )
        if self.tool_candidate:
            return ""
        visible, self.pending = self.pending, ""
        return visible

    def finish(self):
        remaining, self.tail = self.tail, ""
        visible = self._release(remaining)
        if self.tool_candidate and not self.pending.lstrip().startswith("<tool_use"):
            visible += self.pending
            self.pending = ""
        return visible
