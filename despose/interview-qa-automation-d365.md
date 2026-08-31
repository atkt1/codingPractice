# Interview Guide — Senior QA Automation Engineer (D365 UI Test Automation)

**Candidate profile:** ~6y10m QA Engineering · Playwright, Pytest, Postman, JMeter · GitHub Actions + Jenkins · Python/Java/JS · Anthropic "Claude Certified Architect – Foundations, 2026"
**Role context:** `d365-test-automation` — Python + Playwright (sync API) + pytest + Allure, driving Microsoft Dynamics 365 CRM over CDP against a persistent Chrome session, running in a network-restricted devpod/CI.

---

## 0. How to run this

| Block | Minutes | Purpose |
|---|---|---|
| A. Calibration & resume probe | 10 | Establish what they personally did |
| B. Playwright & Python depth | 20 | Core craft |
| C. Framework & architecture | 15 | Can they own our four-layer design |
| D. CI/CD in a restricted network | 10 | Our actual pain |
| E. AI-assisted engineering | 15 | Test the cert against real work |
| **F. Scenario-based hands-on** | **30** | **The deciding block** |
| G. Live exercise (optional 2nd round) | 45 | Verify the code is theirs |

**Scoring convention used below:** 🟢 = strong signal, 🟡 = acceptable, 🔴 = concern. Grade each block, not each question.

> **A note on the certification.** Treat "Claude Certified Architect – Foundations" as evidence of *interest*, not of *ability* — it is a foundations-level credential and you have no independent view of its rigour. Section E is written to test working knowledge directly, so the cert neither helps nor hurts the candidate on its own. Do not let it substitute for demonstrated skill, and do not discount the candidate for holding it.

---

## A. Calibration & resume probe (10 min)

Open neutrally. The aim is to separate *what the team did* from *what this person did*.

1. Walk me through the Vcheck automation suite. **How many tests, how long did a full run take, and who else committed to it?**
   - 🟢 Concrete numbers, names collaborators, describes their own slice honestly.
   - 🔴 Everything is "I built the whole framework" with no numbers.

2. Your resume cites **40% faster regression, 35% less UAT rework, 25% better response time, 15% more server capacity, 60% code efficiency, 80% fewer post-release issues.** Pick the one you're proudest of — how was the baseline measured, and who validated the after-number?
   - 🟢 Names a measurement method, admits which figures were estimates.
   - 🔴 Cannot reconstruct any baseline. (Common on rewritten resumes — probe once, don't hammer.)

3. Dates: Infosys Sep-2019 → Feb-2024, Vcheck Mar-2024 → Dec-2025. **What have you been working on since Dec 2025?**
   - Neutral, factual question. Listen for an honest answer; a gap is not a defect.

4. Your resume totals experience at 6y10m; the listed roles span about 6y4m. Anything earlier I should know about?
   - Low stakes. You are checking care with detail, not catching them out.

5. You list Selenium, Robot Framework, and Playwright. **Which did you choose yourself, and which were inherited?** If you were starting our D365 suite today, which would you pick and why?
   - 🟢 Picks Playwright, and the reasons are technical (auto-waiting, CDP, tracing, one language) not fashionable.

---

## B. Playwright & Python depth (20 min)

6. We use the **sync** API, not async. What do you lose, and when would that hurt?
   - 🟢 Knows sync runs a greenlet loop under the hood; loses concurrency inside a single test; sync is fine because pytest is sync and our parallelism is process-level (`pytest-xdist`), not coroutine-level. Notes you cannot mix sync API into an async event loop.

7. Explain Playwright's **auto-waiting**. Which actions wait, and what exactly does "actionable" mean?
   - 🟢 Attached, visible, stable (not animating), receives events (not covered), enabled. Notes `locator.click()` waits but `element_handle` semantics and `query_selector` do not.

8. When is an explicit wait still necessary despite auto-waiting?
   - 🟢 Waiting on *state you assert*, not on actionability — a grid finishing a fetch, a toast disappearing, a spinner clearing, a network response. `expect(...).to_have_text()` with a timeout over `time.sleep()`.

9. `locator` vs `element_handle` — why does Playwright push locators?
   - 🟢 Locators are lazy and re-resolve on every use, so they survive re-renders; handles go stale. Critical for D365, which re-renders constantly.

10. What actually causes **flake**, and how do you tell a flaky test from a real intermittent bug?
    - 🟢 Refuses to auto-quarantine. Wants traces/video/retry-with-evidence, then classifies. 🔴 "Add a retry and move on."

11. Walk me through debugging a failure you only see in CI. What do you collect?
    - 🟢 Playwright trace, screenshot on failure, console + network logs, Allure attachment, seed/test-data state.

12. Pytest: explain `fixture` scopes and when a `session`-scoped browser fixture becomes a liability.
    - 🟢 State bleed between tests; failure in one test poisoning later ones; xdist gives one per worker, not one globally.

13. How do you make a suite safe to run with `pytest-xdist -n 4` against **one shared CRM tenant**?
    - 🟢 Unique test data per worker, no shared record mutation, no reliance on ordering, careful with global search/filters. This is a genuinely hard problem — reward candour over a glib answer.

---

## C. Framework & architecture (15 min)

Our layering is **Test → Actions → Page Object → Locators**.

14. Draw that on the whiteboard from the names alone. What belongs in each layer?
    - 🟢 Test = intent + assertions only. Actions = business workflows composed of page methods. Page Object = interactions with one page/component, no assertions. Locators = selectors only, zero logic.

15. A step needs a 5-second wait for a D365 grid to settle. **Which layer owns that wait, and why not the others?**
    - 🟢 Page Object. Tests must not know about timing; Locators hold no behaviour; Actions orchestrate, they don't wait on DOM internals.

16. Should a Page Object method assert? Defend your answer.
    - 🟢 No — it returns state; the test asserts. Accepts a pragmatic exception for guard-rail preconditions, and says so explicitly.

17. Two features need 80% of the same flow with a different final step. How do you avoid both duplication and an unreadable inheritance tree?
    - 🟢 Composition, parameterised action, or a small builder. 🔴 Deep base-class hierarchies.

18. Our navigation paths live in a YAML data file (`navigation_paths.yaml`) rather than in code. Argue **against** that decision, then tell me whether you'd keep it.
    - Tests whether they can critique a design honestly. 🟢 Notes the costs — no type checking, no IDE nav, drift between YAML and reality, harder refactors — then makes a reasoned call.

---

## D. CI/CD in a restricted network (10 min)

Our runners are a **devpod with no public internet**; packages come from an internal **Nexus** mirror. The container image and the Playwright version must be pinned together.

19. Why must the Playwright **Python package version** and the **browser/container image** match? What breaks if they drift?
    - 🟢 Browsers are downloaded per Playwright version; a mismatch gives "executable doesn't exist" or subtle protocol errors. Knows `playwright install` is version-coupled.

20. You need a new library mid-sprint and the devpod has no internet. What's your sequence?
    - 🟢 Check Nexus first, request the mirror if absent, and *meanwhile* solve it with the stdlib. 🔴 "I'd pip install it" or "I'd vendor a wheel from my laptop."

21. A CI job **hits its timeout and is killed.** Your Allure results were going to be uploaded in the last step. What do you get, and how do you fix that permanently?
    - 🟢 Immediately spots that a killed job produces **no artifacts** — the exact failures you most need evidence for are the ones you lose. Fixes: upload as an `always()`/post step, write results incrementally, set the *test* timeout below the *job* timeout so the run fails gracefully. **This is a top discriminator — a candidate who has really lived in CI gets it in seconds.**

22. GitHub Actions vs Jenkins — you've used both. Where is Jenkins still the right answer?
    - 🟢 Long-running jobs, on-prem/network-locked agents, heavy shared credentials, existing plugin estate. Not a religious answer either way.

23. Where do CRM credentials live, and how do they reach a test at runtime without ever hitting a log or an Allure report?
    - 🟢 Secret store / masked CI secrets, injected as env vars, never logged, redaction in reporting hooks. 🔴 "In a config file in the repo."

---

## E. AI-assisted engineering (15 min)

Part of this role touches an LLM-driven test-authoring agent. Test working knowledge; ignore the badge.

24. Where have you actually used Claude Code or a coding agent on real work? Show me something it got **wrong** and what you did about it.
    - 🟢 Concrete, specific, unflattering. 🔴 Only success stories, or only demo-scale usage.

25. What is the difference between a **prompt**, a **system prompt / instruction file**, and a **tool** the model can call? Why does the distinction matter for reliability?
    - 🟢 Tools are deterministic code with a contract; the model chooses *when* to call them but not *what they do*. Reliability comes from shrinking the judgement surface.

26. If you needed an agent to behave the same way twice, would you write prose instructions or a decision table? Why?
    - 🟢 Decision table with first-match-wins ordering, explicit fallback, and a defined terminal state. Understands that small models follow tables and improvise badly.

27. **How do you test an agent?** It isn't deterministic in the usual sense.
    - 🟢 Test the tool layer with ordinary unit tests; test the instruction layer by walking scenarios and checking exactly one action is defined at every decision point; check every reachable tool output has a handling rule; check the run always terminates. 🔴 "Run it a few times and see."

28. An agent writes a test that passes. **Would you merge it?** What's your review checklist?
    - 🟢 Verifies the assertion is meaningful (a passing test that asserts nothing is worse than none), the locator is stable, it's in the right layer, it fails when it should. Wants to see it go red before it goes green.

29. What would make you *not* use an agent for a piece of test work?
    - 🟢 Anything needing domain judgement, anything touching production data, anything where a plausible-but-wrong answer is expensive. A candidate with no limits here is a concern.

---

## F. Scenario-based questions — hands-on, grounded in this project (30 min)

> Give the candidate the scenario verbatim. Let them ask questions — **the questions they ask are worth as much as the answer.**

### S1 — The shared browser 🔥

> Our tool attaches to a **already-running Chrome** over CDP (`connect_over_cdp`) because logging in to the corporate D365 tenant is manual and SSO-gated. A new contributor adds `browser.close()` in a teardown fixture. CI goes green. The next morning the whole team is blocked.
>
> **What happened, why did CI stay green, and what do you change so it can't happen again?**

- 🟢 Understands `connect_over_cdp` returns a browser **you do not own** — closing it kills the human's session and forces a manual re-login. Green CI because the teardown ran *after* assertions. Fix: close only *your* context/page, never the browser; add a lint/grep guard or wrap connection in a helper that has no `close`; document the ownership rule.
- 🟡 Gets the cause but only proposes "code review".
- 🔴 Doesn't distinguish `launch()` from `connect_over_cdp()`.

### S2 — The confident false positive 🔥

> A helper scans the page for a list of UI labels and reports which were found and which were missing. It matched **case-insensitive substring**. Someone searched for `Task` on a D365 home page and got **five** results — all left-nav items like *My Tasks and Appointments* — and the tool reported `missing: []`.
>
> **Why is this worse than returning nothing? What's your fix, and what does your fix break?**

- 🟢 Names it: a **false positive is more dangerous than a false negative** because downstream logic proceeds confidently on a wrong element. Fix: exact match, with a narrow, explicit allowance for decoration (trailing punctuation, a keyboard-shortcut suffix like `Save (Ctrl+S)`). Then — unprompted — states the cost: **"missing" now means something different**, previously-passing lookups will start failing, so it must ship together with whatever consumes it, and needs a rollback path.
- 🟡 Says "use exact match" and stops.
- 🔴 Suggests fuzzy matching or a similarity score. (Ranking replaces one guess with another.)

### S3 — Locators under constraint

> Our locators must be **plain CSS / attribute selectors only**. No XPath, no `role=`, no `:has-text()`, no text-based Playwright pseudo-selectors — the resolver uses `document.querySelectorAll` and nothing else.
>
> Here is a D365 command-bar button:
> ```html
> <button id="ms-button-8231" data-id="task|NoRelationship|HomePageGrid|NewRecord"
>         aria-label="New" class="ms-Button ms-Button--commandBar root-104">
>   <span class="ms-Button-label label-108">New</span>
> </button>
> ```
> **Write the locator you'd commit. Then rank your alternatives worst to best and justify the order.**

- 🟢 Chooses `[data-id="task|NoRelationship|HomePageGrid|NewRecord"]` — semantic, D365-stable, survives re-render. Rejects `#ms-button-8231` (generated, changes per render) and `.root-104` / `.label-108` (Fluent UI hash classes, change on every build). Accepts `[aria-label="New"]` as a reasonable fallback but notes it is localisation-fragile.
- Bonus 🟢: asks whether the `data-id` is stable across *environments*, not just across renders.
- 🔴 Reaches for XPath or `:has-text()` after being told they're unavailable.

### S4 — Nested iframes

> A test fails with "element not found." The element is visibly on screen. D365 renders the form inside a nested iframe.
>
> **How do you diagnose and handle it, and what's the trap?**

- 🟢 Knows `page.frame_locator()` / `frame` traversal, that selectors don't cross frame boundaries, and that D365 uses `#contentIFrame0` / dynamically-named form frames. Trap: **the frame may not exist yet** when you look, and cross-origin frames may be unreadable. Handles unreadable frames by *disclosing* them rather than silently reporting "not found" — a partial survey must never claim to be complete.
- 🟡 Knows frame_locator but not the timing/partial-read trap.

### S5 — Flaky at 30%

> A test passes locally every time and fails ~30% of the time in CI, always on the same step: clicking a row in a freshly-loaded D365 grid.
>
> **Give me your diagnosis order.** You may ask three questions before you start.

- 🟢 Good questions: is CI slower/headless/different viewport? Does the grid do a second async fetch after first paint? Is another worker mutating the same records? Diagnosis order: trace first, then compare timings, then check whether the click landed on a *stale row* that re-rendered under the cursor. Fix: wait on a **settled** condition (row count stable / network idle / a specific cell's text), not `sleep`.
- 🔴 Jumps straight to increasing the timeout or adding retries.

### S6 — Test data on a shared tenant

> Tests run nightly with `-n 4` against one shared D365 sandbox. Test A creates a Contact named "Test User" and asserts a global search returns exactly one result. It's been passing for months. It now fails about once a week.
>
> **What's happening, and how do you fix it properly?**

- 🟢 Other workers / prior runs left "Test User" records behind. Real fix: unique data per run (worker id + timestamp or UUID in the name), assert on *your* record not on a global count, and clean up in teardown — while acknowledging teardown doesn't run if the job is killed, so uniqueness must carry the load, not cleanup.
- 🔴 "Add a retry" or "delete all contacts before the run" (destructive on a shared tenant).

### S7 — Performance testing D365 ⚠️ *(trap question)*

> Your resume shows JMeter load testing with strong results. Leadership asks you to **load test our D365 CRM** to find the response-time ceiling.
>
> **How do you approach it?**

- 🟢 **Pushes back before scoping.** D365 is Microsoft-hosted SaaS on shared infrastructure — load testing it is governed by Microsoft's terms and their testing/notification process, not something a team just runs; and the numbers wouldn't be actionable anyway since we don't control the tier or the capacity. Redirects to what *is* legitimate and useful: measure our own layer — plugin/workflow execution time, custom API endpoints, integrations we own — and use client-side timing (Playwright trace, Navigation Timing) to track *user-perceived* page performance as a regression signal.
- 🟡 Scopes it competently but never questions whether it's allowed.
- 🔴 Enthusiastically designs a 5,000-user ramp against the production tenant. **This is a judgement red flag, not a knowledge gap** — willingness to run destructive load against someone else's infrastructure without asking is the thing you're screening for.

### S8 — The dropdown that isn't

> `select_option()` works in one D365 form and silently does nothing in another.
>
> **Why, and how do you write one helper that handles both?**

- 🟢 One is a native `<select>`; the other is a custom Fluent UI combobox — a button that opens a rendered listbox. `select_option()` only works on native selects. Helper: detect the tag first, branch, and **verify after** by reading back the displayed value. Notes the verification must handle a control that *reformats* what it displays (`Task` → `Task (active)`), so strict equality will report an unverified-but-correct selection — better to surface that than to silently accept a wrong one.
- 🟡 Gets the branch but no post-verification.

### S9 — Assert a negative

> An acceptance criterion says: *"The Save button must be disabled until a required field is filled."*
>
> **Write the assertion and tell me how it could pass while the feature is broken.**

- 🟢 `expect(locator).to_be_disabled()`. False-pass modes: the button isn't rendered yet so it "isn't enabled"; it's `aria-disabled` but still clickable; the locator matches a *different* Save button (command bar vs form footer). Guards: assert the element is **visible first**, then disabled; assert the click has no effect; pin the locator to the right container.
- 🟢🟢 Volunteers that the test must be seen to **fail** against a build where the field is filled.

### S10 — Layering under pressure

> It's the day before a release. A test fails because a D365 dialog now takes longer to appear. The fastest fix is one line in the test file: `page.wait_for_timeout(3000)`.
>
> **Do you ship it?**

- 🟢 Ships a *scoped* fix if genuinely needed — but a wait on a **condition** (`expect(dialog).to_be_visible(timeout=...)`) in the **Page Object**, not a blind sleep in the test. Says out loud that a hard sleep is both slower and less reliable, and that if a sleep does ship it gets a ticket and a deadline, not a shrug.
- 🔴 Either "never, principles" (inflexible) or "sure, it's one line" (no standards). You want the middle.

### S11 — Reviewing someone else's automation

> A contractor delivers 60 new tests. All pass. Coverage numbers look great.
>
> **What do you check before merging, in priority order?**

- 🟢 First: **do they fail when they should?** Break something deliberately and re-run. Then: are assertions meaningful or just `assert page is not None`; are there try/except blocks swallowing failures; hardcoded sleeps; hardcoded record IDs; brittle generated-class locators; layering violations; are they independent and order-independent.
- 🟢🟢 Mentions mutation-style checking — a test that cannot fail is negative value: it costs runtime and manufactures false confidence.

### S12 — Communicating a regression

> Your nightly run goes from 4 failures to 47 overnight. Nothing in your repo changed.
>
> **What do you do in the first 30 minutes, and what do you tell the team?**

- 🟢 Triage before escalating: is it one root cause fanned out (a login change, an environment refresh, a D365 platform update, an expired credential) or 47 distinct issues? Check one failure end-to-end first. Communicates early with *what is known and what isn't* — resists both silence and panic.
- 🔴 Files 47 bugs, or reruns until it's green.

---

## G. Live exercise — optional second round (45 min)

Give a real page and a laptop. Pair, don't watch silently.

> **Task:** Here is a D365 form. Write a Page Object plus one test that fills two fields, saves, and verifies the record was created. Constraints: sync Playwright API, CSS-only locators, our four-layer structure, no `time.sleep`.
>
> **Then:** I'll change one field's HTML. Make your test pass again — and tell me whether your original locator *should* have survived.

Watch for: where they put things, whether they run the test red first, whether they name things well under time pressure, and whether they ask about the data setup. **How they behave when stuck matters more than whether they finish.**

---

## Signals summary

**Green flags**
- Distinguishes a false positive from a false negative and treats the first as worse
- Pushes back on S7 before scoping it
- Immediately spots the killed-job/no-artifacts problem in Q21
- Says "I don't know" cleanly, then reasons toward an answer
- Critiques a design they've been told is ours (Q18)
- Wants to see a test fail before trusting it

**Red flags**
- Retries and `sleep` as the answer to flake
- Can't reconstruct any resume metric
- Treats the certification as the answer to Section E
- Designs the load test in S7 without a moment's hesitation
- No examples of an AI tool being wrong

---

## Scorecard

| Block | Weight | Score /5 | Notes |
|---|---|---|---|
| A. Calibration | 5% | | |
| B. Playwright & Python | 20% | | |
| C. Architecture | 15% | | |
| D. CI/CD restricted network | 10% | | |
| E. AI-assisted engineering | 10% | | |
| **F. Scenarios** | **40%** | | |

**Recommendation:** ☐ Strong hire ☐ Hire ☐ Hire with reservations (specify) ☐ No hire

**Bar for this role:** Section F is the decision. A candidate can be shaky on trivia and still be excellent if they reason well through S1, S2, S5 and S7 — and a candidate can recite Playwright documentation perfectly and still be wrong for a suite that runs unattended against a shared corporate tenant.
