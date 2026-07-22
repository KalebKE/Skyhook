# Benchmarking Skyhook honestly

Most "how many tokens did it save" numbers for tools like this are misleading, and
Skyhook's own headline number used to be one of them. This doc explains what is easy to
measure wrong, and a method that measures the thing that actually matters: what a real
coding agent does differently when Skyhook is wired in.

## Two things people measure wrong

1. **Artifact size is not adoption.** `skyhook bench` compares the size of a route pack
   (~2–5k tokens) against reading the files it points at (~14–18k), and reports a "4–5x
   context reduction." That ratio is real, but it measures the *artifact*: it silently
   assumes the agent reads the pack *instead of* grepping and re-reading. An agent that
   ignores the pack realizes none of it. Adoption has to be measured separately.

2. **The saved tokens are mostly cached anyway.** Agentic sessions are dominated by the
   model re-reading its accumulated (cached) context every turn. Cache reads cost a small
   fraction of fresh input, so even a large reduction in the *orientation* slice nets a
   modest change in the total bill. A raw token multiplier over-states the dollar effect.

So a route pack being "Nx smaller than the files" tells you about the pack, not about
what an agent does with it. To learn that, you have to run an agent.

## The ground-truth method

Take a real bug that was really fixed, with a test that catches it. Revert the fix, hand
the agent the symptom (not the location), let it fix the bug, and grade by running the
test. Do it with and without Skyhook wired in. This gives an objective correct/incorrect
per run, plus cost and exploration.

The details that make it valid:

- **Pick a bug whose fix location is non-obvious** — the symptom shows up far from the
  fix (a data-flow / wiring / concurrency bug), so an agent that greps the symptom can
  land in the wrong place. A bug localized to one obviously-named file is too easy to tell
  anything.
- **The test must not reference symbols the fix introduced**, or reverting the fix stops
  the test compiling. Prefer fixes that change existing logic, graded by a behavioral test.
- **Commit the buggy state.** If you only revert in the working tree, the agent will find
  the fix one `git checkout HEAD -- <file>` away and "solve" it in one turn. Commit the
  revert so `HEAD` *is* the bug. Better still, base the checkout at the fix's parent commit
  and graft in only the test, so the fix commit is not even in `git log`'s ancestry.
  Exclude any run that reads the fix out of git history (`git show`/`log`/`checkout <ref>`).
- **Wire Skyhook with `alwaysLoad: true`.** Without it the MCP server connects after the
  session starts, its tools never enter the toolset, and the agent silently falls back to
  grep — you end up measuring a disconnected server, not Skyhook. `skyhook init` writes a
  ready `.skyhook/mcp.json` with this set.
- **Grade with the bug's own test**, not by eyeballing the diff. Any behaviorally-correct
  fix passes, wherever the agent put it.

## What it actually showed

Measured on a real ~594k-line, 24-module production monorepo, two bugs (one localized, one
a non-obvious concurrency bug), baseline vs Skyhook, 4 runs per side, graded by the bugs'
own unit tests. Agent: Claude Code on Sonnet.

- **Correctness: no change.** Both conditions produced correct fixes on both bugs
  (baseline 4/4 and 4/4; Skyhook 3/4 and 4/4, the single miss an unrelated non-compiling
  serializer). On the hard bug, *every* run — baseline included — found the right file. A
  capable modern agent locates a non-obvious fix given enough exploration; Skyhook did not
  make fixes more correct.
- **Efficiency and consistency: real.** On the hard bug, Skyhook runs averaged ~32% lower
  cost and about half the turns — but the bigger effect was variance. Baseline ranged from
  10 to 65 turns (one run cost 2x the others); every Skyhook run was 13–18 turns. The route
  pack gives a reliable starting point, so the agent does not rabbit-hole. The localized
  bug showed the same shape at smaller magnitude (~26% cheaper).
- **Adoption: reliable once connected.** With `alwaysLoad`, the agent called `route` first
  on every Skyhook run.

## The honest takeaway

Skyhook's measured value is not correctness, and not a headline token multiplier. It is
**efficiency and predictability**: on the order of a quarter to a third less cost, roughly
half the turns, and — most usefully — it clamps the worst-case exploration that otherwise
makes agent cost unpredictable. Per task that is modest. Across many tasks, a consistent
cost reduction plus a tighter variance is a real, defensible win, and it is one you can
reproduce with the method above rather than take on faith.

Caveats: both bugs were ultimately solvable by the baseline agent, so a bug the baseline
genuinely cannot locate might still show a correctness benefit — these did not produce one.
n is small (4 per side per bug); the effect is consistent but not precise. Results are for
one agent and one model; a weaker model may lean on Skyhook more.
