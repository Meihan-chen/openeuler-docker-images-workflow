# Failure knowledge policy

The machine-readable knowledge base is `docs/failure-patterns.yml`. This file
documents its maintenance policy; it is not an append-only incident log.

Only generalized patterns that satisfy all of these conditions belong in the
knowledge base:

1. The symptom can be matched against Harness-produced evidence.
2. The diagnosis and remediation are application-neutral.
3. At least one reproducible workflow artifact or regression test verifies the
   pattern.
4. A deterministic generation gate is recorded as prevention when one exists.

Raw incidents, application-specific commands, copied logs, speculative fixes,
and truncated PR summaries do not belong here. Keep those in workflow evidence
or an implementation record and cite them from the structured entry when they
verify a generalized rule.

Pattern IDs are stable semantic names. Update an existing pattern when its
diagnosis becomes more precise; add a new pattern only for a distinct root-cause
class. Duplicate symptoms with the same remediation must be merged.
