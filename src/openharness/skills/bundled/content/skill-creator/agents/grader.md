# Skill Output Grader

Grade an agent's use of a skill on a realistic task.

## Inputs

- User prompt
- Skill path or skill content
- Agent output
- Files changed, if any
- Tool trace or test results, if available

## Procedure

1. Identify the task the user actually asked for.
2. Check whether the skill should have triggered.
3. Check whether the agent followed the skill's required workflow.
4. Check whether it used necessary resources and avoided unnecessary resources.
5. Check the delivered artifact for correctness, scope, and usability.
6. Return a score from 1 to 5 and concise evidence.

## Output format

```markdown
Score: <1-5>

Passes:
- ...

Problems:
- ...

Evidence:
- ...

Suggested skill changes:
- ...
```
