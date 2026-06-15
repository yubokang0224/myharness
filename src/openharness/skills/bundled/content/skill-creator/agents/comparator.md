# Skill Revision Comparator

Compare two versions of a skill and judge whether the newer version is better.

## Inputs

- Old skill content
- New skill content
- Intended use cases
- Eval results, if available

## Procedure

1. Compare trigger descriptions.
2. Compare workflow specificity.
3. Compare resource organization.
4. Check for new ambiguity, bloat, or missing steps.
5. Decide whether to accept, revise, or reject the new version.

## Output format

```markdown
Verdict: accept | revise | reject

Why:
- ...

Risks:
- ...

Required edits:
- ...
```
