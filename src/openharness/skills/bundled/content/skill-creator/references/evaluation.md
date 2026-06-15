# Skill Evaluation

Use evaluation when a skill is important, ambiguous, or likely to be reused.

## Eval design

Create tasks that look like real user prompts. Include:

- A straightforward case that should obviously trigger the skill
- A realistic edge case
- A near miss that should not broaden the skill beyond its scope
- A case that requires loading one resource, if resources exist

Avoid giving the evaluator your expected answer unless the goal is exact-output
checking. For workflow skills, judge whether the agent took the right steps and
used the right resources.

## Forward-testing loop

1. Run the candidate skill on realistic tasks.
2. Capture prompt, output, files changed, and tool trace when available.
3. Grade with `agents/grader.md`.
4. If activation failed, inspect the description with `agents/prompt-analyzer.md`
   or `scripts/improve_description.py`.
5. If a revision is made, compare before/after with `agents/comparator.md`.
6. Aggregate results with `scripts/aggregate_benchmark.py`.

## Rubric

Score each run from 1 to 5:

- 5: Correctly triggered, used appropriate resources, produced usable output.
- 4: Mostly correct with small omissions.
- 3: Partly useful but missed an important step or resource.
- 2: Triggered but followed the wrong workflow.
- 1: Failed to trigger or produced harmful/confusing guidance.

Track notes as evidence, not vibes. Cite file paths, output fragments, and
observable mistakes.

## Iteration rules

- If the wrong prompts trigger the skill, narrow the description.
- If the right prompts do not trigger the skill, add concrete trigger language.
- If the body is too long, move conditional detail into `references/`.
- If agents keep rewriting the same helper code, add a script.
- If agents misuse a script, add usage examples near the script reference.
