# Skill Trigger Analyzer

Analyze whether a skill description is likely to trigger for the right prompts.

## Inputs

- Skill name
- Current description
- Example prompts that should trigger
- Example prompts that should not trigger, if available

## Procedure

1. Extract the action verbs and domain nouns in the description.
2. Compare them with the user's likely wording.
3. Identify missing trigger phrases.
4. Identify wording that is too broad or could cause false positives.
5. Suggest one replacement description under 120 words.

## Output format

```markdown
Likely misses:
- ...

Likely false positives:
- ...

Replacement description:
...
```
