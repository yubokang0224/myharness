# Skill Schema And Location

## Required shape

Directory skills use this shape:

```text
<skill-name>/
  SKILL.md
```

`SKILL.md` must begin with YAML frontmatter. OpenHarness reads at least:

- `name`: skill identifier
- `description`: trigger text shown to the model before the body is loaded

Keep frontmatter minimal. Extra fields may be ignored by OpenHarness and may not
be portable across runtimes.

## Naming

- Use lowercase letters, digits, and hyphens.
- Keep names under 64 characters.
- Prefer action-oriented names: `review-pr`, `create-dashboard`, `fix-ci`.
- Match directory name and `name` whenever possible.
- Avoid names that collide with built-ins unless intentional.

## Description

The description is the main trigger. It should answer:

- What does this skill help with?
- Which user phrases or task contexts should activate it?
- Which important variants are covered?

Good descriptions include concrete nouns and verbs from user prompts. Avoid a
generic description like "Helps with documents" when the skill should only handle
redline review or contract clause extraction.

## Body

The body should be procedural. Use it for:

- Workflow steps
- Required checks
- Tool or file conventions
- Risk boundaries
- Pointers to resources

Do not repeat broad "when to use" text in the body; by the time the body is
loaded, activation already happened.

## Optional resources

- `scripts/`: deterministic helpers, validators, converters, packagers
- `references/`: longer docs, schemas, examples, policies
- `agents/`: evaluator or helper-agent prompts
- `assets/`: templates, static examples, icons, fonts, fixture files

Keep resources one level from `SKILL.md` and name them clearly. If a resource is
large, include a table of contents at the top.

## OpenHarness Target Location

For this product, create and update local skills here:

```text
.openharness/skills/<skill-name>/SKILL.md
```

If the skill includes resources, put them next to `SKILL.md`:

```text
.openharness/skills/<skill-name>/
  SKILL.md
  scripts/
  references/
  agents/
  assets/
```

Repository built-in skills are an implementation detail. Only edit
`src/openharness/skills/bundled/content/` when the user explicitly asks to change
the built-in skills shipped with OpenHarness.

The loader registers skills by name. If the same name appears more than once,
the later registration wins in the registry.

## Validation checklist

- The file is UTF-8 Markdown.
- Frontmatter starts and ends with `---`.
- `name` and `description` are non-empty strings.
- Directory name and `name` intentionally match.
- Description includes real trigger phrases.
- Instructions are specific enough for another agent to follow.
- Referenced resource paths exist.
- Scripts run on the target platform.
