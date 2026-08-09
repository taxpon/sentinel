# Playbook — `dep-upgrade`

> **Status:** Design · **Answers:** What standing context does every `security-dep` and
> `frontend-dep` remediation start with?

| | |
|---|---|
| Playbook name | `dep-upgrade` |
| Classes served | `security-dep`, `frontend-dep` |
| `max_acu_limit` | 10 |
| Created | By hand in the Devin UI ([B6](../blockers.md#b6)); id supplied via `DEVIN_PLAYBOOK_IDS` |

Paste the block below verbatim as the playbook body. See [`README.md`](./README.md) for the section
structure and for what belongs here rather than in the prompt.

```text
## Overview

An advisory against a pinned dependency, and the call-site changes the upgrade
forces. Changing the version number is the smallest part of this work and never
the part that is being judged.

## Procedure

1. **Establish why the pin is where it is, before choosing a target version**: a
   pin that sits behind a published fix is usually held there deliberately. The
   reason is a comment on the pin itself, a cap in pyproject.toml, or a transitive
   dependency that still calls an API the new major removed. Finding that
   constraint is the task; the bump is what happens afterwards.

2. **Find the second-order records that name the package by major version**: they
   live in sections a version bump would not otherwise touch. The liccheck
   allowlist under [tool.liccheck.authorized_packages] is one of them, and a diff
   that misses it fails the licence check for a reason that has nothing to do with
   the advisory.

3. **Confirm the fixed version yourself**: an advisory that records only a
   last-affected version does not tell you which release fixed it, and the issue
   may hand you an inferred answer and say that it is inferred.

4. **On the frontend, derive the scope from import sites rather than from the
   audit's list**: families such as deck.gl and luma.gl require matching majors
   across their subpackages, so remediating one forces all of them, and the code
   that breaks lives in the plugins that import the family — not in the flagged
   package, which may have no import site anywhere in the tree.

5. **Adapt the call sites the upgrade forces, because that is what creates the
   evidence**: test the paths the upgrade made you touch — the key-loading and
   transport paths for an SSH library, the plugin's own render and layer tests for
   a frontend family bump. Those are the assertions that would have caught a
   two-major jump going wrong.

6. **Regenerate lockfiles and compiled requirements with the project's own
   tooling** and commit the result.

## Advice & Pointers

What CI will tell you, and what it cannot.

Python: changing pyproject.toml, setup.py or requirements/*.txt runs the whole
tests/unit_tests suite. That is the broadest signal any remediation class gets
here, and for a multi-major jump it is the best evidence available. It is worth
the wall clock.

Frontend: superset-frontend/package.json and superset-frontend/package-lock.json
map to no test target at all. A diff that changes only manifests and the lockfile
runs jest on nothing, and the check suite still concludes green with a warning
that no test target could be derived. The jest signal exists only for packages
whose source files you actually edited. Bump and stop, and the green tick means
nothing.

The frontend type-check runs nowhere in this CI — the type-checking-frontend hook
is skipped in the workflow. Run it locally before you push, because a major bump
is precisely the change that breaks types without breaking a test.

Neither signal exercises the vulnerable code. Nothing in Superset's test suite
feeds a malformed image to an image parser or an SHA-1 signature to an SSH
transport. What this CI can prove is that the upgrade did not break Superset.
Write that in the pull request in those words.

If the upgrade genuinely required no source change, the honest report is that the
evidence is a regenerated lockfile and a suite that still passes. Say so in
root_cause and set confidence to match.

root_cause: why the dependency was still on the vulnerable version — the
constraint that held it there, and what you did about that constraint. "The pin
was capped below the fixed major because a transitive dependency still imports a
key type that major removed" is a root cause. "Upgraded from 3.5.1 to 5.0.0" is a
changelog entry.

Budget: 10 ACUs, the smallest of the four playbooks, and dependency resolution is
where it disappears fastest. If the resolver will not converge, or the transitive
blocker has no release that clears it, report outcome "blocked" naming the
specific constraint and what would have to change. For this class that is a
complete answer, not a failure.

## Forbidden actions

- Hand-editing a lockfile or a compiled requirements file. It is the most common
  way this class produces a build nobody can reproduce.
- Treating the audit's package list as the scope of a frontend family bump.
- Leaving structured_output.tests.added empty on the grounds that a version
  constraint is the whole change. A version constraint is not a test.
- Inventing an assertion that exercises nothing the upgrade touched, so that the
  field is non-empty.
- Raising or removing a cap without first establishing what it was holding back.
- Writing the pull request as though the vulnerability itself had been
  demonstrated closed.
```
