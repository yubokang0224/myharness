---
name: skill-creator
description: >
  Create, update, validate, package, and improve OpenHarness-compatible Agent Skills.
  Use when asked to design or generate a skill, SKILL.md, .openharness/skills
  entry, or Claude/Anthropic-style skill; also use when adapting a skill from
  anthropics/skills for this agent.
---

# skill-creator

Create concise, reusable skills for OpenHarness while staying compatible with the
public Agent Skills directory format.

## Resource loading

This skill has bundled resources next to `SKILL.md`. When a task needs them,
locate this skill directory first, then read or run only the relevant file:

- `references/skill-schema.md`: file layout, metadata rules, and the .openharness/skills target
- `references/evaluation.md`: forward-testing and eval loop guidance
- `agents/grader.md`: independent output-quality grading rubric
- `agents/prompt-analyzer.md`: trigger and scope analysis rubric
- `agents/comparator.md`: compare two skill revisions
- `scripts/quick_validate.py`: validate a skill folder or bundled skill file
- `scripts/package_skill.py`: package a directory skill into a zip
- `scripts/improve_description.py`: inspect example prompts and suggest a better description
- `scripts/aggregate_benchmark.py`: summarize eval run results
- `scripts/generate_report.py`: create a local HTML eval report

For simple skill creation, do not load all resources. Use this `SKILL.md` only.

## Core format

A portable skill is a directory named after the skill, containing a required
`SKILL.md`:

```text
skill-name/
  SKILL.md
  agents/      optional evaluator or helper-agent prompts
  scripts/     optional deterministic helpers
  references/  optional docs loaded only when needed
  assets/      optional templates or static resources
```

`SKILL.md` must start with YAML frontmatter:

```markdown
---
name: skill-name
description: What the skill does and exactly when to use it.
---

# Skill Name

Instructions the agent follows after the skill activates.
```

Use lowercase letters, digits, and hyphens for `name`; keep it under 64
characters; make the directory name match it. The `description` is the activation
surface, so include concrete trigger words and contexts there, not only in the
body.

## OpenHarness Location

Create runtime skills in one canonical location:

```text
.openharness/skills/<skill-name>/SKILL.md
```

Use this path for new skills unless the user explicitly asks to edit this
repository's built-in skill set. If maintaining built-in skills in this source
tree, directory-style bundled skills live under
`src/openharness/skills/bundled/content/<skill-name>/SKILL.md`.

Prefer directory skills when the skill needs scripts, references, assets, or
agent prompts.

## Workflow

1. Understand the real task examples.
   - Ask only if the use cases, target location, or required resources are
     genuinely unclear.
   - Prefer extracting patterns from existing project files, tests, runbooks, or
     prior corrections.
2. Pick a focused scope.
   - A skill should cover one coherent workflow.
   - Split broad domains into separate skills or into `references/` files with
     clear load conditions.
3. Decide resource shape.
   - Put deterministic or fragile repeated logic in `scripts/`.
   - Put detailed, conditional knowledge in `references/`.
   - Put reusable evaluator prompts in `agents/`.
   - Put templates, boilerplate, images, or static inputs in `assets/`.
   - Do not add README, changelog, install notes, or extra docs unless the
     runtime needs them.
4. Write the skill.
   - Keep `SKILL.md` lean, ideally under 500 lines.
   - Start with concrete steps and defaults.
   - Include gotchas the agent is likely to miss.
   - Use examples or output templates only when they materially improve
     reliability.
5. Validate.
   - Run `scripts/quick_validate.py <path-to-skill>` for directory skills.
   - For OpenHarness repository changes, run focused pytest coverage around
     skill loading, such as `python -m pytest tests/test_skills/test_loader.py`.
   - For `.openharness/skills` skills, load the registry or use the UI/API skill list to
     confirm the intended name and description appear.
6. Iterate from actual runs.
   - If the agent failed because instructions were vague, make the procedure
     more specific.
   - If the agent followed irrelevant instructions, narrow the description or
     move conditional detail to references.
   - If the same code keeps being rewritten, add a script.

## Writing rules

- Add only what the agent would otherwise get wrong or waste time rediscovering.
- Prefer procedures over generic advice.
- Provide defaults instead of long menus of equivalent options.
- Match strictness to risk: prescriptive steps for fragile operations, guidance
  for flexible judgment work.
- Reference files with paths relative to the skill root.
- Avoid deeply nested reference chains; link needed references directly from
  `SKILL.md`.
- Preserve user changes and existing project style when updating an existing
  skill.

## OpenHarness gotchas

- For this product, create skills under `.openharness/skills/`.
- The runtime may still support other locations for compatibility, but do not
  choose them unless the user explicitly asks.
- The skill registry can include built-in and local skills. Avoid accidental
  name collisions.
- The current `skill` tool returns `SKILL.md` content only. If a resource file is
  needed, locate the skill directory on disk before reading or executing it.

## When to load references

- Load `references/skill-schema.md` before creating a skill with resources or
  changing loader behavior.
- Load `references/evaluation.md` before designing eval tasks, grader prompts,
  or an iteration loop.
- Load an `agents/` prompt only when using an independent pass to assess output,
  description quality, or a revision comparison.
