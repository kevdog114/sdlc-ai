# Agents, Prompts & Local-Model Design

A focused review of the agent definitions, the prompts that actually run, and what it would take to
make this a *self-improving* team that works well with *local* models. Companion to
[REVIEW.md](REVIEW.md); references are to `7ee5435`.

The headline: the role/persona system is a good design skeleton that **isn't plugged into the path
that runs**, the prompts are written for a strong instruction-follower rather than a local model,
and the feedback substrate (event log, knowledge base) exists but **no loop closes over it**. All
three are fixable, and the fixes are cheap relative to their leverage.

---

## 1. The structural problem: two competing prompt systems

There are two entirely separate ways an agent's instructions get built, and they don't agree.

**System A — the role YAMLs** (`roles/*.yaml`, assembled by `build_system_prompt` in
`core/runtime.py:135`). Rich and thoughtfully structured: `system_prompt` + `cognitive_profile` +
`behavioral_instructions` (rules, preferred_format) + `operational_constraints`. `business_analyst.yaml`
even carries an exact markdown output template (`:38-45`).

**System B — inline prompts** (`project_tool.py:138,216,280`; `stage_gate_tool.py:434,454`;
`orchestrator_tool.py:86`). Plain f-strings with no reference to the YAMLs.

**The pipeline that actually runs (Engine A) uses System B exclusively.** The BA phase runs
`BA_ANALYSIS_PROMPT`, not `business_analyst.yaml`; the Architect phase runs `ARCHITECT_DESIGN_PROMPT`,
not `architect.yaml`. The role YAMLs are loaded only for their bare `system_prompt` string, and only
for the QA and Architect *gates*. So:

- The carefully-designed persona files are **decorative** for the main path. Editing
  `business_analyst.yaml` to improve BA behavior changes nothing.
- There are two sources of truth for "what is a BA," which will drift.
- `preferred_model`, `execution_engine`, `cognitive_profile`, `max_loop_depth`, and `risk_threshold`
  are read by essentially nothing (confirmed in the roles review).

**Fix:** pick one system. The right one is the YAMLs — externalized, per-role, versionable (which
matters enormously for self-improvement, §4). Make `project_tool`'s phases load and render the role
definitions instead of carrying their own inline copies. Then a prompt improvement is a data edit,
not a code change.

---

## 2. Prompt-craft issues (independent of which system wins)

**2.1 · The ReAct system prompt shows every tool to every agent.** `build_system_prompt` lists all
~15 `AVAILABLE_TOOLS` to every persona (`runtime.py:139-141`); `allowed_tools` is enforced only at
call time as an "Access Denied" (`runtime.py:214`). So a developer sees `opencode_*`, `knowledge_*`,
memo tools it may not use, picks one, wastes a turn on a denial. For a local model, every extra tool
is a chance to go wrong and a slice of context spent. **Filter the tool list to `allowed_tools`
before rendering** — the persona spec even calls for this (`persona_spec_v2.md`), it just wasn't
done.

**2.2 · Persona "flavor" costs context without changing behavior.** `cognitive_profile:
reasoning_style: deep_dive`, `communication_style: concise_technical` and similar are injected as
prose (`runtime.py:146-166`). These are vibes, not instructions a model can act on, and they inflate
the per-turn boilerplate. On a strong model they're harmless; on an 8–32k local model they crowd out
the task. **Keep the actionable `rules`; drop the abstract profile**, or collapse it into one line.

**2.3 · Three different output contracts, each parsed fragilely.** JSON ("Return ONLY valid JSON"),
ReAct (`ACTION:`/`ARGS:`/`FINAL ANSWER:`), and gate verdicts (`PASS -`/`APPROVE -`). Each is enforced
by the most brittle possible parser: a greedy regex for JSON (`_extract_json`, `project_tool.py:32`),
a single-line split for ReAct args (`worker.py:196` — breaks on any multi-line JSON, i.e. every real
`file_write`), and a substring test for verdicts (`stage_gate_tool.py:113` — the P0 bug). Format
adherence is the *hardest* thing for local models; this system pairs the loosest instruction with the
least-tolerant parser. See §3 for the structural fix.

**2.4 · The gates ask models to judge code they cannot see.** `QA_GATE_PROMPT` asks "Does the
implementation address the task?" but is handed only `{description}`, `{agent}`, and the developer's
own `{notes}` (`stage_gate_tool.py:436-439`). No prompt can make a model evaluate an implementation
that isn't in its context — it will confabulate a verdict. This is a prompt-design impossibility, not
just the wiring bug from REVIEW P0-3. **Feed the gate the actual diff/files and test output.**

**2.5 · Inconsistent, often-too-high temperatures.** `query_llm` defaults to `0.7` (`llm_tool.py:41`);
planning prompts use `0.3`; the ReAct loop uses `0.0`. For structured/agentic/JSON work, `0.7` is too
high and invites malformed output. **Default low (0.0–0.2) for anything parsed; let a role opt into
higher temperature only for genuinely generative steps.**

**2.6 · What's already good** (keep it): the QA persona's adversarial framing ("look for what could
break"), the researcher's "note uncertainty rather than fabricating," the contract-first block
injected into the developer prompt (`opencode_tool.py:109`), and the gates' clear verdict *format*
(it's the parsing that's wrong, not the prompt).

---

## 3. Making it work well with local models

This is where the largest, cheapest wins are. Local models (LM Studio / llama.cpp / Ollama) differ
from frontier APIs in three ways that this codebase currently fights: weaker instruction-following,
smaller context, and — crucially — **the ability to *constrain* output that the code doesn't use.**

**3.1 · Use the server's structured-output / grammar constraints. (Highest leverage.)** LM Studio and
llama.cpp can force output to valid JSON via a GBNF grammar or a JSON schema (`response_format` /
`json_schema`). The code uses none of it — it asks politely for JSON and then regex-scrapes the
result (`_extract_json`), which on a local model fails routinely. Passing a schema to the endpoint
turns ~60% parse success into ~100% and deletes an entire class of "silently fell back to one generic
task" failures. `query_llm` should grow a `response_schema` parameter and pass it through to the
`/chat/completions` payload.

**3.2 · Add a parse-repair loop.** When output still doesn't validate, re-ask once with the error
("Your previous output was not valid JSON; return only the JSON matching this schema"). Today a parse
failure silently degrades (`decompose_goal` collapses a whole project into a single developer task;
`_ba_analyze` treats the raw text as the requirements). One retry recovers most local-model slips.

**3.3 · Prefer native tool-calling over string-parsed ReAct.** The hand-rolled
`THOUGHT/ACTION/ARGS` loop with line-based parsing is the single most local-model-hostile part of the
design (`worker.py:196`, `runtime.py:241`). Local servers increasingly support OpenAI-style `tools`/
`tool_calls`; using it moves the format burden from the model's prose into the server's constrained
decoder. If native calling isn't available for a given model, at least parse a fenced ```json block
rather than a single line.

**3.4 · Implement the model-routing tiers — this matters *more* locally, not less.** The README
describes high/medium/low tiers; nothing implements them (everything hits one model, `runtime.py:388`
hardcodes `model="local"`). Locally you often *cannot* hold two large models in VRAM at once, so
routing must be deliberate:
- Wire up the dead `preferred_model` field so each role picks its model.
- Route by task/complexity: decomposition and architecture → the strongest model you can load;
  implementation → a coding model; **role-selection, summarization, verdict, and clarification
  parsing → a small fast model** (these do not need a 27B model and currently waste one —
  `orchestrator_daemon._get_available_role`, `knowledge_tool._generate_summary`).
- Make routing swap-aware (sequential model loads) rather than assuming concurrent availability.

**3.5 · Cut the per-turn context budget.** Every ReAct turn re-sends persona + full profile + all
tool docs + protocol + growing history. That's 1–2k tokens of boilerplate before task content,
re-sent each turn, against an 8–32k window that degrades as it fills. Combine 2.1 + 2.2, and add
history trimming/summarization, so the model spends its window on the work.

**3.6 · Stop hardcoding model names that look real but aren't.** `DEFAULT_MODEL =
"qwen/qwen3.6-27b"` and the self-test's `"google/gemma-4-26b-a4b"` (`llm_tool.py:19,224`) are
placeholder identifiers; if they don't match what's actually loaded in LM Studio the call fails.
Take the model id from config only, and fail loudly with the list of available models rather than
defaulting to a fictional one.

---

## 4. Making it a *self-improving* team

Everything needed to learn is already being recorded — `logs/event_log.jsonl` captures every action,
gate result, and rejection, and `knowledge_tool` stores insights. **What's missing is any loop that
reads it back.** The knowledge base is write-mostly; the roles are static; nothing analyzes outcomes.
Four loops, cheapest first:

**4.1 · Retrieval-augmented planning (cheapest win).** Before the BA/Architect prompt runs, query the
knowledge base for the top-k relevant past insights and inject them ("Patterns from similar past
work: …"). Today the planning prompts get only the current description; the team's accumulated
experience sits unused on disk. `query_knowledge` already exists — call it from the planning phase,
not just from inside agent loops.

**4.2 · Structured rejection mining.** When a gate rejects, store the reason as a *typed* record
(category, role, task type), not a free-text blob in `verification_artifacts`. Then "tasks from role
X fail the QA gate 40% of the time for reason Y" becomes a query. That single statistic drives both
prompt fixes and routing decisions — and it's computable from data you already emit.

**4.3 · The retrospective loop (the core of self-improvement).** After each sprint (see
[SCRUM_PROCESS.md](SCRUM_PROCESS.md) §4.4), an analysis agent reads the event-log slice + rejection
records and proposes concrete, versioned edits to: (a) a role's prompt, (b) a Definition of
Ready/Done criterion, or (c) a routing rule. Each proposal names the metric it expects to move.
This is the mechanism that turns "a pipeline that runs once" into "a team that compounds."

**4.4 · Versioned, measurable prompts.** For 4.3 to be safe, role prompts must be **data, not code**
(hence §1's "make the YAMLs authoritative"), each with a version and a linked outcome metric
(acceptance rate, rejection rate, cost per accepted point). Then a proposed prompt change can be
A/B'd against the prior version and reverted if it regresses. Without versioned prompts, "self-
improvement" has nothing to improve and no way to know if it helped.

---

## Priority order

1. **Constrained output + repair loop** (§3.1, §3.2) — biggest local-model reliability win, small change.
2. **Fix the gate verdict parser and feed gates real artifacts** (§2.3, §2.4 / REVIEW P0-1, P0-3) —
   without this, no feedback signal is trustworthy, so self-improvement would learn from noise.
3. **Make the role YAMLs authoritative and filter tools per role** (§1, §2.1) — unifies the prompt
   system and makes prompts editable data.
4. **Implement model routing / wire up `preferred_model`** (§3.4) — the local-hardware multiplier.
5. **Close one feedback loop: RAG planning first, then rejection mining, then the retrospective**
   (§4.1 → §4.2 → §4.3).
