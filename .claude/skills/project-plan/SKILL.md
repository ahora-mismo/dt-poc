---
name: project-plan
description: Use when creating implementation plans. Defines plan structure, ADR sections, TDD approach, deviation tracking, and task list format.
---

# This is how we handle plans:
- we will do a multistep implementation of the plan
- **Phase 1 — Discussion & ADR:** we discuss implementations and ADR decisions. No code in the plan, only high-level changes and affected files listed. Ask the user to proceed to phase 2.
- **Phase 2 — Full code in the plan:** add the full implementation code to the plan (before/after blocks, file names, change markers). For each task, include a short description of what to test (e.g. "test that stale version is dropped"), but NEVER write actual unit test code in the plan. Unit test code never goes in the md file.
- **Phase 3 — Implementation:** when the user explicitly asks to implement, pass tasks to subagents. If using claude, use sonnet for subagents, if using other LLM, use a similarly capable one. Subagents implement the code exactly as written in the plan AND write the unit tests themselves following TDD red-green, guided by the test descriptions from phase 2. All rules from below will be respected.
- phases must be implemented in order
- there is a single exception for skipping phase 2, if we have an empty project, then we directly implement phase 3, but with the main more capable agent, nut with lesser agents.

## File structure:
- plans must sit inside project root: `/PLANS/<YYYY-mm-dd>-<short-kebabcase-summary>/plan-<same short-kebabcase-summary>.md`
- when writing code (phase 2), all code must be written outside the plan, in a subdirectory at the same level as the plan md file: `./src/<real folder path to where we plan the file>/<affected file name including extension>.md`
- the md code files contain diff-format code
- all code must sit in this file-folder structure, not inside the plan. the plan may show snippets inside if useful, but not more
- `src/` diff files contain ONLY code diffs — no test descriptions, no explanations, no metadata. Test descriptions belong in the task list inside the plan markdown, not in the diff files.

## General rules:
- sections of the plan, in this order: Title, Summary, ADR, Approved Decisions, (whatever else, add it in here, can be multiple subsections), Implementation and steps, Task list, Deviations, TBDs -- feel free to choose different names, but this is the section order
- each ADR must contain the following subsections: a title, a status line, context, decision and consequences
- ADR status line format: `**Status:** ✅ Approved by user` — no date/time; the only datetime in a plan is the top "last updated" field. Add `**Source:** TBD-<n>` when the ADR came from a TBD.
- plans must have on top a last updated at in EET, each time we change the plan we update that field. we must use real date from user's computer
- we will always use a red-green TDD approach for implementing the plan, the plan must contain that information for the LLM to know later on
- the plan must contain complete code for the implementation, including file names and short test descriptions (what to test, not the test code). Actual unit test code is NEVER part of the plan — subagents write it during implementation.
- **New files:** show the full file code.
- **Modifications to existing files:** use diff-style — show only the changed lines with 3-5 lines of surrounding context, plus the file path and line number marker (e.g. ` CHANGE: line 42-58 in src/a/b.ext`). Do NOT duplicate the entire function or file as before/after blocks. If the change is a single line, just show that line with its context.
- **Don't repeat unchanged code across tasks:** if Task 3 modifies a file that Task 1 already showed in full, Task 3 should only show the diff — not the entire file again. Reference "file as created in Task 1, with the following change:" then show only the diff.
- never ask the user to implement the plan, never implement the plan by yourself, the plan will be implemented exclusively when the user asks explicitly for that.
- open questions and TBD items go in a dedicated `## Open Tasks / TBD` section at the very end of the plan. each TBD item must have: a short title, a description of the problem, any research done so far (options, trade-offs), and a `**Decision: TBD**` marker. TBDs must be resolved before the related implementation task can start.
- the user is the architect, do not take decisions (or ADRs), but your input is very much appreciated, propose solutions but ask for acceptance.
- we must have an ADR section at the top, having all decisions, reasoning, and solution
- once a TBD is resolved, the TBD must be removed and integrated inside the plan. If it needs a line in ADR, add it in there, but not all TBDs are significant enough for ADR. If unsure if it needs to be added to ADR, ask. TBD will not be renumbered, this is completely forbidden, gaps will remain and this is expected.
- **every resolved TBD or user decision must be recorded as approved**, in exactly one of two places:
  - **ADR** — significant/architectural decisions (full ADR format, with status line).
  - **Approved Decisions** — minor decisions not worth an ADR. Each entry: `D-<n>: <short title>`, the decision in 1-3 lines, `Source: TBD-<n>` (if any), and `✅ Approved by user` (no date/time). D-numbers follow the same no-renumbering rule as TBDs.
- only the user approves. Claude's proposals stay in the TBD section until the user explicitly accepts them; never add an ADR or Approved Decision entry, or mark one approved, without explicit user acceptance. The ✅ marker is the proof that the user (architect) approved it.
- a decision can be promoted from Approved Decisions to ADR (or demoted) only with user agreement.
- **no junk in the plan:** the plan states the current state only. No history or meta notes: no "revised by the user", "an earlier version…", "originally researched in…", "user direction", "user said/wants", "(to confirm)", strikethrough options, "no longer viable", "parked by the user", or format explanations of the plan itself. When a decision changes, rewrite it in place; when an option dies, delete it. Proposals are labeled "Proposal", nothing else.

# Implementation tasks:
- at the end of the plan, always break it into a numbered task list. each task is a discrete implementable unit. when the user asks to implement the plan, take one task at a time and mark completed tasks as done (no timestamp needed, just mark them).
- tasks are short labels — implementation details belong in the plan sections and src/ diff files, not in the task list.
- each task includes a short description of what to test (e.g. "test that stale version is dropped"). This is the only place test descriptions live — not in diff files, not elsewhere in the plan.
- when dispatching a task to a subagent, always include the task's test descriptions from the plan in the subagent prompt. The subagent needs to know what to test — it does not have access to the plan file. The subagent must implement all described tests but is free to add more tests if it identifies additional cases worth covering.
- you will show the task list in console and update their status real time, for the user to see. this is mandatory.
- you can mark the task status in the plan md file at the end, we don't need to have this real time updated
- resolved tasks must show green check mark emoji in the task list
- all tasks must be numbered, not with markdown auto numbers, every number must be explicitly correct in the source.

# Deviations:
- deviations are things we found after we started implementing the plan
- when implementing the plan, you are not allowed to automatically implement deviations. we must implement the code exactly as it is in the plan
- if we find something we need to change, and it is not in the plan, DO NOT stop or wait for user input. Continue with all other tasks that are not blocked by the deviation. Only report deviations at the end, after all non-blocked tasks are completed. Add deviation tasks on TBD section
- deviations must contain full code proposals, and TBDs will reference deviations, each deviation must be a new tbd for the user to decide against.
- you will tell about them to the user in the console, so it will know there's still something left
- solved deviations are to be removed from deviation section and integrated into the plan
- we will keep a deviation history at the end, short version of it.