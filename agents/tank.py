"""
TANK — Elite Programming Specialist

Pure router. ALL actual code generation, explanation, debugging, and optimization
logic lives in actions/code_helper.py and actions/dev_agent.py — TANK does not
duplicate any of it. TANK's only job is:
  1. Work out which of the five capabilities the request needs.
  2. Pull out any parameters present in the natural-language request
     (file path / inline code / language / error message).
  3. Call the existing engine with those parameters.

Capabilities (all backed by actions/code_helper.py unless noted):
  1. Code solution          -> code_helper(action="write"/"auto") or dev_agent() for full builds
  2. Algorithm explanation   -> code_helper(action="explain", mode="algorithm")
  3. Line-by-line breakdown  -> code_helper(action="explain", mode="line_by_line")
  4. Debugging               -> code_helper(action="debug")
  5. Optimization suggestions -> code_helper(action="optimize")
"""
from agents.base_agent import ShadowAgent, AgentReport
from agents._llm import generate_json


CLASSIFY_PROMPT = """Classify this programming request for TANK, BERU's coding specialist, and
extract any parameters present. Return ONLY valid JSON:
{"capability": "...", "file_path": "", "code": "", "language": "", "error_output": ""}

capability — exactly one of:
  CODE_SOLUTION          — write new code, or build/scaffold something, from a description
  ALGORITHM_EXPLANATION  — explain the algorithm/approach/complexity behind existing code
  LINE_BY_LINE           — a line-by-line / statement-by-statement breakdown of existing code
  DEBUGGING              — fix a bug or error in existing code
  OPTIMIZATION           — improve the performance/readability of existing code

file_path     — an exact file path mentioned in the request, else "".
code          — raw code included verbatim in the request (e.g. pasted in), else "".
language      — programming language if stated or strongly implied, else "".
error_output  — an error message/traceback included verbatim in the request, else "".

Only fill file_path / code / error_output if they are actually present in the request text —
never invent or guess them.
"""


class Tank(ShadowAgent):
    NAME  = "TANK"
    TITLE = "Elite Programming Specialist"

    CAPABILITIES = (
        "CODE_SOLUTION", "ALGORITHM_EXPLANATION", "LINE_BY_LINE",
        "DEBUGGING", "OPTIMIZATION",
    )

    # Signals a full multi-file project build rather than a single-file task.
    BUILD_KEYWORDS = (
        "build a project", "build an app", "build me an app", "create a project",
        "full project", "multi-file", "complete application", "new app",
        "scaffold", "from scratch", "entire system", "whole project",
    )

    _DEBUG_KEYWORDS    = ("debug", "fix this bug", "fix the bug", "not working",
                           "crashes", "crashing", "traceback", "throws an error",
                           "error in my code")
    _ALGO_KEYWORDS     = ("algorithm", "time complexity", "big o", "big-o",
                           "approach does this use", "how does this work")
    _LINE_KEYWORDS     = ("line by line", "line-by-line", "walk me through",
                           "explain each line", "explain every line")
    _OPTIMIZE_KEYWORDS = ("optimize", "speed up", "make faster", "performance",
                           "refactor", "improve this code")

    def handle(self, goal: str, speak=None) -> AgentReport:
        data = self._classify(goal)
        capability   = data.get("capability", "CODE_SOLUTION")
        file_path    = data.get("file_path", "")
        code         = data.get("code", "")
        language     = data.get("language", "")
        error_output = data.get("error_output", "")

        print(f"[TANK] 🛠️ capability={capability} file_path={bool(file_path)} code={bool(code)}")

        try:
            if capability == "CODE_SOLUTION":
                result = self._code_solution(goal, language, speak)
            elif capability == "ALGORITHM_EXPLANATION":
                result = self._explain(file_path, code, "algorithm")
            elif capability == "LINE_BY_LINE":
                result = self._explain(file_path, code, "line_by_line")
            elif capability == "DEBUGGING":
                result = self._debug(file_path, code, error_output, goal, language)
            elif capability == "OPTIMIZATION":
                result = self._optimize(file_path, code, language)
            else:
                result = self._code_solution(goal, language, speak)
        except Exception as e:
            return self._report(f"TANK encountered an error: {e}", success=False)

        label = capability.replace("_", " ").title()
        return self._report(f"{label} complete, Master.", str(result))

    # ------------------------------------------------------------------ #

    def _classify(self, goal: str) -> dict:
        data = generate_json(
            f"Request: {goal}",
            model_name="gemini-2.5-flash-lite",
            system_instruction=CLASSIFY_PROMPT,
        )
        if not isinstance(data, dict) or data.get("capability") not in self.CAPABILITIES:
            data = {
                "capability": self._fallback_capability(goal),
                "file_path": "", "code": "", "language": "", "error_output": "",
            }
        return data

    def _fallback_capability(self, goal: str) -> str:
        low = goal.lower()
        if any(k in low for k in self._DEBUG_KEYWORDS):
            return "DEBUGGING"
        if any(k in low for k in self._LINE_KEYWORDS):
            return "LINE_BY_LINE"
        if any(k in low for k in self._ALGO_KEYWORDS):
            return "ALGORITHM_EXPLANATION"
        if any(k in low for k in self._OPTIMIZE_KEYWORDS):
            return "OPTIMIZATION"
        return "CODE_SOLUTION"

    def _require_source(self, file_path: str, code: str) -> bool:
        return bool(file_path or code)

    # ------------------------------------------------------------------ #
    # Each of these is a thin call into actions/code_helper.py or
    # actions/dev_agent.py — no generation/fix/explain logic lives here.
    # ------------------------------------------------------------------ #

    def _code_solution(self, goal: str, language: str, speak):
        if any(k in goal.lower() for k in self.BUILD_KEYWORDS):
            from actions.dev_agent import dev_agent
            return dev_agent(parameters={"description": goal}, speak=speak)

        from actions.code_helper import code_helper
        params = {"action": "auto", "description": goal}
        if language:
            params["language"] = language
        return code_helper(parameters=params, speak=speak)

    def _explain(self, file_path: str, code: str, mode: str):
        if not self._require_source(file_path, code):
            return "I need either a file path or the code itself to explain it, Master."
        from actions.code_helper import code_helper
        return code_helper(parameters={
            "action": "explain", "mode": mode,
            "file_path": file_path, "code": code,
        })

    def _debug(self, file_path: str, code: str, error_output: str, goal: str, language: str):
        if not self._require_source(file_path, code):
            return "I need either a file path or the code itself to debug it, Master."
        from actions.code_helper import code_helper
        return code_helper(parameters={
            "action": "debug",
            "file_path": file_path, "code": code,
            "error_output": error_output, "description": goal,
            "language": language,
        })

    def _optimize(self, file_path: str, code: str, language: str):
        if not self._require_source(file_path, code):
            return "I need either a file path or the code itself to optimize it, Master."
        from actions.code_helper import code_helper
        params = {"action": "optimize", "file_path": file_path, "code": code}
        if language:
            params["language"] = language
        return code_helper(parameters=params)
