# Interview Set 2 — Claude / Agent Engineering, Applied to Our Framework

**Purpose:** test whether the candidate's Anthropic certification translates into judgement that helps *this* codebase.
**Companion to:** `interview-qa-automation-d365.md` (core QA/Playwright/CI). This set assumes that one covers the fundamentals; it does not repeat them.
**Duration:** 60–75 min. **Format:** hands-on — print the artifacts, hand them over, let the candidate read and mark them up.

---

## What we are actually testing

Our stack is not "we use AI to write tests." It is a real agent system with three parts:

1. **`agent_harvest.py`** — a CLI tool over CDP. It is the agent's *entire* browser vocabulary. Whatever it can emit is exactly what the model can ever see. One invocation = one action = one JSON object on stdout.
2. **`fieldAutomationAgent-V13.x.md`** — a ~12,300-word instruction file: 14 hard rules and four first-match-wins decision tables.
3. **Claude Haiku 4.5** — the model executing it. Small, fast, cheap. Follows explicit decision tables well and **improvises badly.**

So the skills that matter are: designing a tool contract a weak model cannot misread, putting guardrails in code rather than prose, managing context budget, and testing something that isn't deterministic in the usual sense.

> **Calibrating the certificate.** "Claude Certified Architect – Foundations" is a foundations-level credential and you have no independent view of its rigour. These questions deliberately test **transfer, not recall** — none of them can be answered from exam material. Expect either failure mode: someone who passed the exam but has never shipped an agent, or someone who has shipped plenty and forgot the vocabulary. **Grade the reasoning, not the terminology.** If they use a different word for the right idea, that's a pass.

---

## Block 1 — Model and cost judgement (10 min)

**Q1.** We run this on **Haiku 4.5**, not Sonnet or Opus. Someone proposes upgrading to fix quality complaints.
**Make the argument for staying on Haiku, then tell me the one condition under which you'd escalate.**

- 🟢 Cost and latency per invocation matter because the loop runs dozens of times per issue. More importantly: **a bigger model hides a bad instruction file rather than fixing it** — if a step needs a smarter model to be reliable, the step is under-specified, and it will still be under-specified on the bigger model, just failing less visibly. Escalate only for a step that genuinely needs open-ended judgement (e.g. reading an ambiguous acceptance criterion), and escalate **that step alone**, not the whole pipeline.
- 🟡 Argues cost only.
- 🔴 "Bigger model, better results" with no analysis.

**Q2.** Which parts of our pipeline would you move to a larger model, and which would you move **out of the model entirely** into plain code?

- 🟢 Out of the model: anything with a decidable answer — selector validation, version checks, dedup, capping, ordering, file writes. Into a larger model: interpreting a free-text acceptance criterion, or a final review pass. Understands that **every judgement call you delete is a source of run-to-run variance you delete.**

**Q3.** The instruction file is ~12,300 words — call it ~16k tokens — and it is part of the input on **every** turn. Twenty issues a week, ~40 tool invocations each.
**How do you keep this affordable, and what breaks your solution?**

- 🟢 Prompt caching on the stable prefix (instruction file + tool definitions), so the repeated bulk is read from cache rather than reprocessed at full price. Puts the **stable content first** and the volatile content last, because a cache hit requires an identical prefix.
- 🟢🟢 Names what breaks it: **any edit to the instruction file invalidates the cache**, and so does anything volatile accidentally placed above the stable block (a timestamp, a run id, per-run context). Also notes caches expire, so a low-traffic period gets no benefit.
- 🔴 No concept of caching, or thinks the file is sent once per session.

---

## Block 2 — The tool boundary (20 min) 🔥 *highest-yield block*

### S1 — Read this tool output

> Hand them this. It is a real shape our scanner emits.

```json
{
  "action": "scan",
  "status": "ok",
  "termsFound": ["new"],
  "termsMissing": ["assign", "close task"],
  "matches": [
    {
      "term": "new",
      "region": "commandBar",
      "family": "button",
      "actionable": true,
      "sels": [{"css": "[data-id=\"task|NoRelationship|HomePageGrid|NewRecord\"]", "n": 1}]
    }
  ]
}
```

> The model reads this and writes a test asserting that **"Assign" does not exist on this page.**
> **What has gone wrong, and how do you fix it at the tool layer?**

- 🟢 `termsMissing` is doing two incompatible jobs: it means "genuinely absent" *and* "I couldn't find it." A page that hadn't finished loading, a frame that couldn't be read, or a control behind a collapsed menu all land in the same bucket — and the model, seeing `status: 'ok'`, has no way to know. **A tool must never report ignorance as knowledge.**
- 🟢🟢 Fix: split the field. Absent-and-confident vs. unresolved-and-unknown must be **different fields with different names**, so the decision table can route them differently. (This is exactly why our real output has `termsFound` / `termsFoundHidden` / `termsUnresolved` / `termsMissing` as four disjoint fields.)
- 🟡 Spots the problem but fixes it in the prompt ("tell the model to be careful") rather than in the schema.
- 🔴 Sees nothing wrong.

### S2 — The constraint the model won't respect

> Our selector resolver runs `document.querySelectorAll` and nothing else. XPath, `role=`, `:has-text()` and Playwright's text pseudo-selectors **cannot** work. The instruction file says so in a hard rule, in bold, twice.
> The model still emits `get_by_role("button", name="Save")` maybe one time in fifteen.
>
> **Why does prose fail here, and what do you do instead?**

- 🟢 The model has an enormous prior from training: public Playwright documentation and examples are saturated with `get_by_role`. You are asking a small model to suppress a strong habit using instructions alone — that will leak at some rate, forever, and the rate isn't stable across page contexts.
- 🟢🟢 Fix at the boundary: **validate in the tool.** Reject a non-CSS selector before it executes, return a typed error the decision table already handles, and let the model correct itself from a mechanical signal rather than from memory. Prose is a *preference*; code is a *guarantee*.
- 🟢🟢🟢 Adds the general principle: **anything you cannot afford to have happen must be impossible, not discouraged.**
- 🔴 "Repeat the rule more forcefully" / "add it to the system prompt again."

### S3 — Guardrail placement

> Two safety rules in our system:
> **(a)** never call `browser.close()` — the Chrome session is a human's SSO-authenticated login and killing it costs a manual re-login;
> **(b)** never `pip install` anything — the devpod has no internet.
>
> Both are currently written as hard rules in the instruction file.
> **Rank how each should actually be enforced, and say what you'd do this week.**

- 🟢 Neither belongs in prose alone. (a): don't expose a close capability at all — the tool has no such verb, so the model cannot reach it; add a CI grep as a second net. (b): the environment already enforces it (no network), so the real work is making the *failure* legible — a typed dependency error that routes to a defined STOP, instead of the model retrying an install forever.
- 🟢🟢 Articulates the hierarchy: **environment > tool surface > code validation > instruction file.** Prose is the last line, not the first.

### S4 — Design a tool contract

> We need a new capability: **assert an element is disabled.**
> **Design the tool's output contract on the whiteboard.** What fields, what statuses, what happens on each failure mode — for a model that will *only* see this JSON and nothing else.

- 🟢 Enumerates real states before designing: found+disabled, found+enabled, found but `aria-disabled` while still clickable, not found, multiple matches, frame unreadable, page never settled. Gives each a **distinct, machine-checkable** signal — a `status` plus a typed `code` — rather than a prose `message`.
- 🟢🟢 Says explicitly that **"not found" must not collapse into "disabled"** — the most dangerous failure is the one that looks like a pass.
- 🟢🟢🟢 Asks: "does every one of these have a handling row in the instruction file?" — because an output with no matching row means the run halts.
- 🔴 Returns `{"ok": true}` or a free-text string.

### S5 — Prose in, prose out

> A junior writes this tool return:
> ```python
> return "Could not find the element, maybe try a different selector or check the frame"
> ```
> **What will a small model do with it? Rewrite it.**

- 🟢 A small model will *act on the suggestion* — it reads "try a different selector" as an instruction and starts guessing, unboundedly. Prose in a tool result becomes prompt injection from your own codebase.
- 🟢🟢 Rewrite: `{"action": "probe", "status": "failed", "code": "SELECTOR_NOT_FOUND", "framesRead": 3, "framesSkipped": 1}` — facts only, no advice. **The tool reports state; the instruction file decides what to do about it.** Keeping those separate is what makes behaviour reviewable.

---

## Block 3 — Instruction design and determinism (20 min) 🔥

### S6 — Find the defect

> Hand them this table. Tell them the file's rule is **"first matching row wins."**

| # | Result | Action |
|---|---|---|
| 1 | `status: 'ok'` | Record the selector and proceed to the next step |
| 2 | `status: 'ok'` with `framesSkipped` present | Disclose the unread frames, then proceed |
| 3 | `status: 'failed'` | STOP and report |

> **What's wrong?**

- 🟢 **Row 2 is unreachable.** Every result that matches row 2 also matches row 1, and row 1 comes first — so the disclosure never happens, and a partial survey gets silently reported as a complete one. Fix: reorder (most specific first), or make row 1's condition exclusive.
- 🟢🟢 Generalises: in any first-match-wins table, **a later row whose condition is a superset-match of an earlier row is dead code** — and unlike dead code in a program, nothing warns you. Wants this checked mechanically, not by eye.
- 🟡 Spots it only after a hint.

*(This was a real bug in our file. We now have an automated `check_row_shadowing` guard because of it.)*

### S7 — The dangerous kind of ambiguity

> Two rules from the same instruction file:
>
> - *"Budget: at most 3 scans per page. No exceptions."*
> - *"If any term comes back unresolved, re-run the scan once for those terms."*
>
> The third scan returns unresolved terms.
> **What does the model do? Why is this worse than a rule that's simply missing?**

- 🟢 Undefined — and crucially, **each rule looks individually complete**, so the model never realises it should ask. It picks one, confidently, and different runs pick differently. A *missing* rule produces a visible halt you can fix; **two complete-looking rules produce silent divergence you only notice statistically.**
- 🟢🟢 Fix: make the interaction explicit — either the retry is inside the budget and the rule says so, or the exhausted case has its own terminal row. Never leave two rules to negotiate.
- 🟢🟢🟢 Notes this is the single highest-yield defect class when reviewing agent instructions, and that finding it requires *walking scenarios*, not reading the file top to bottom.

*(This was the one blocker found by our last determinism walk; it shipped as a fix in V13.5.0.)*

### S8 — Testing something non-deterministic

> **How do you test this system?** You cannot diff the output against a golden file — the model doesn't produce identical text twice.

- 🟢 Splits the problem cleanly:
  - **Tool layer** — ordinary deterministic unit tests. Same input, same JSON. This is most of the surface and it should be tested like any other Python.
  - **Instruction layer** — walk scenarios and check a decidability property at each decision point: *is exactly one action defined?* Plus reachability (every output the tool can emit has exactly one live handling row), and termination (every path ends in a defined terminal state).
  - **End to end** — a small fixed acceptance set, judged on outcomes rather than transcripts.
- 🟢🟢 Mentions **mechanical linting of the instruction file itself** — table structure, row shadowing, version pinning, dangling cross-references. Anything checkable by a script shouldn't be checked by a human twice.
- 🔴 "Run it a few times and see if it looks right."

### S9 — Non-determinism triage

> Same GitLab issue, same D365 page, two runs an hour apart. Run 1 produces a clean test. Run 2 goes down a different path and gives up.
>
> **Is that the model or the tool? What's your first move?**

- 🟢 **Diff the tool outputs first.** If the JSON differs between runs, the non-determinism is in the environment or the tool — page state, timing, unstable ordering, sampling under a cap — and the model is behaving correctly on different inputs. If the JSON is identical and the decisions diverge, the instruction file left a judgement call.
- 🟢🟢 Calls out **unstable ordering** as an underrated cause: if a tool returns matches in a nondeterministic order, the model's input genuinely differs each run even though the page didn't change. Sorting the output deterministically removes an entire class of "flaky agent" reports.
- 🟢🟢🟢 Wants tool invocations and their raw outputs logged per run, as a precondition for ever diagnosing this.
- 🔴 Blames "AI randomness" and stops.

### S10 — Version coupling

> We changed the matcher from substring to exact. `termsMissing` now means something materially different. The instruction file was updated to match.
>
> **How do you ship this?**

- 🟢 **Atomically.** New script + old instruction file = the model misreads results; new instruction file + old script = it waits for fields that never arrive. Neither half is safe alone.
- 🟢🟢 Enforce it in code, not in the release notes: the script self-reports a version, the instruction file declares a minimum, and a preflight check refuses to run on a mismatch. Plus a one-file rollback that's been tested, not assumed.
- 🔴 "Ship the script first, update the docs after."

---

## Block 4 — Context budget (10 min)

### S11 — Too much page

> A D365 home page produces **412 matches** for the requested terms. Dumping them all would blow the model's context and cost a fortune. We cap at 40.
>
> **How do you choose which 40, and what must the model be told?**

- 🟢 Rejects "the 40 most relevant" immediately — relevance is a guess, and a wrong guess here is invisible. Wants a **structural** rule instead: bucket by region and element family, then round-robin across buckets so no category is silently wiped out, and restore the original document order at the end so results are stable run to run.
- 🟢🟢 **The model must be told the list was truncated** — a sampled list presented as complete is the same false-confidence bug as S1. Truncation needs its own signal and its own handling row.
- 🟢🟢🟢 Notes the classification (found / missing / unresolved) must be computed **before** capping, from the full set — otherwise capping itself manufactures false "missing" results.
- 🔴 "Take the first 40" with no disclosure, or "rank them by similarity score."

**Q12.** More generally: what goes in the always-loaded instruction file, versus what should be fetched only when needed?

- 🟢 Always-loaded: hard rules, decision tables, the tool contract — anything needed to make *any* decision safely. Fetched on demand: reference detail, examples, rarely-hit procedures. Understands the tension — a small model given too much context degrades, and a small model missing a needed rule improvises.

---

## Block 5 — Judgement and safety (10 min)

**Q13.** Someone proposes: *"Let's have Claude review the generated tests and auto-merge the ones it approves."*
**Where does that work, and where does it fail?**

- 🟢 Useful for triage, prioritisation, and flagging — cheap, parallel, catches obvious slop. Fails as a **merge gate**: the reviewer shares the generator's blind spots, so the errors are correlated and the confident-but-wrong cases sail through precisely because they look well-formed.
- 🟢🟢 Better gate: deterministic checks the reviewer can't rationalise past — *does the test fail when the feature is broken?*, does it assert anything meaningful, does it use only permitted locators, is it in the right layer. Then a human on anything that touches shared data.
- 🔴 Full enthusiasm, no failure modes.

**Q14.** The agent is one step away from writing a test that mutates records on a **shared corporate CRM tenant.**
**What do you want in place before that's allowed?**

- 🟢 Uniquely-named data it created itself, no touching pre-existing records, an explicit approval gate before the first mutating action, and a clear boundary between read (safe, automatic) and write (gated). Notes that a mutating action must **refuse** to run on an incomplete survey — if a frame couldn't be read, you don't actually know what you're clicking.
- 🟢🟢 Wants the gate enforced by the tool, not by the model remembering to ask.

**Q15.** Tell me about a time you decided **not** to use an agent for something.

- 🟢 Has a real example with a real reason. 🔴 No limits, or only success stories. A candidate who has genuinely shipped this stuff has scar tissue.

---

## Block 6 — Claude Code in the repo (10 min)

*Practical, day-to-day. If they've only used the API and not Claude Code, say so and grade the reasoning transferred.*

**Q16.** New contributors keep putting waits in the test layer instead of the Page Object. **How do you make the four-layer rule stick** using the tooling — not by nagging in code review?

- 🟢 Project instructions checked into the repo so every session starts with the convention loaded; a reusable skill or command for "add a new page object" so the right shape is the *easy* shape; and — the real answer — **a mechanical check in CI**, because conventions that aren't enforced decay. Bonus for knowing hooks can run a check automatically on edit.

**Q17.** You have a large audit to run across the repo — every locator file checked against our selector rules.
**How do you structure that so it's fast and the results are trustworthy?**

- 🟢 Fan out — independent parallel passes over slices, each returning structured findings, then verify findings before reporting. 🟢🟢 Says the verification step matters more than the search step, because a confident false finding wastes more time than a missed one.

**Q18.** What's the difference between putting knowledge in a **project instruction file**, a **skill**, and a **tool**? Give me an example of each from our repo.

- 🟢 Instruction file: always-on conventions (layering, locator rules). Skill: a packaged procedure loaded when relevant (how to add a new page object; how to run a determinism walk). Tool: deterministic capability the model calls but doesn't implement (the harvest script). The distinction is **always-loaded vs on-demand vs executed** — grade the mental model, not the vocabulary.

---

## Block 7 — Live exercise (25 min, optional second round)

Hand over the real `fieldAutomationAgent` instruction file (or a 3-page extract with two seeded defects) plus the tool's output schema.

> **Task 1 (15 min):** Here is one decision table and the list of every result the tool can emit. Find any output with **no** handling row, and any row that can **never** be reached. Mark them up.
>
> **Task 2 (10 min):** We're adding a `wait_for_grid_settled` capability. Design its output contract and write the decision-table rows that consume it. Every failure mode needs exactly one row.

Seed two defects: one unreachable row (shadowed by a more general row above it), and one tool output with no handling row at all. **A strong candidate finds both and asks about a third case you didn't seed.**

Watch for: do they *enumerate the outputs first* rather than reading the table top-to-bottom? Do they ask what happens on an undefined result? Do they think about termination unprompted?

---

## Signals

**Green**
- Puts hard constraints in code, prose last
- Treats "I couldn't tell" as a distinct result from "it's not there"
- Spots that two complete-looking rules are worse than a missing one
- Wants truncation and partial reads *disclosed*, never silently absorbed
- Diffs tool outputs before blaming the model
- Argues *against* a bigger model
- Has an example of an agent being confidently wrong

**Red**
- Fixes model behaviour by rewording the prompt
- Free-text advice inside tool results
- "Relevance ranking" where a structural rule is needed
- Treats the certification as the answer
- No failure modes for LLM-as-judge
- Cannot say when *not* to use an agent

---

## Scorecard

| Block | Weight | /5 | Notes |
|---|---|---|---|
| 1. Model & cost judgement | 10% | | |
| **2. Tool boundary** | **30%** | | |
| **3. Instruction design & determinism** | **30%** | | |
| 4. Context budget | 10% | | |
| 5. Judgement & safety | 15% | | |
| 6. Claude Code in the repo | 5% | | |

**Bar:** Blocks 2 and 3 are 60% of the weight and they are the job. A candidate who reasons well through **S1, S2, S6, S7 and S9** can be taught our vocabulary in a week. One who scores on terminology but misses those five will produce an agent that works in the demo and diverges in production — which is the exact failure this role exists to prevent.

**The single best question if you only get one:** **S7.** It separates people who have *debugged* an agent from people who have *built* one that happened to work.
