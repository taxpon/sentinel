---
title: The bootstrap capability probe reports a degradation rather than failing on it
status: accepted
date: 2026-08-08
type: process
areas: [devin]
tasks: [T40]
files: [scripts/bootstrap_devin.py]
specs: [docs/09-operations.md]
supersedes:
---

# The bootstrap capability probe reports a degradation rather than failing on it

## Context

The fourth thing `docs/09-operations.md#bootstrap` asks of `make bootstrap-devin` is that it
"reports which optional enterprise endpoints are reachable so the degradation path is known before
the demo, not during it".

Three open blockers are about exactly that. [B5](../blockers.md) records that
`GET /v3/enterprise/metrics/sessions` needs `ViewAccountMetrics` at the enterprise level and that
nobody knows whether our credentials will carry it. [B6](../blockers.md) records the same for
playbook CRUD. [B7](../blockers.md) records that the organisation may validate session tags against
a registered vocabulary. All three are open because no credentials exist to ask with
([B8](../blockers.md)), and the `Degradable` design in `devin/schemas.py` exists precisely because
the answers may be "no".

So the probe has to answer three questions whose answers are not symmetrical: one endpoint can be
called, one must not be, and one cannot be settled by a bootstrap run at all.

## Decision

**The probe reports; only the four required steps can fail the run.** A capability that is refused,
unreachable, or answers with a body the models do not accept becomes a row in a table and leaves
the exit code at 0. Each row carries the reason, the status code, and the fallback sentence quoted
from the degradation table — the same sentence the dashboard labels a derived figure with, taken
from `Capability.fallback` so the two cannot drift.

Per blocker:

- **B5** and the ACU-spend row are probed for real, through `session_metrics()` and
  `daily_consumption()`. Without `DEVIN_ENTERPRISE_ID` the enterprise call is not attempted at all
  and the row reports `not_configured`, which is itself the answer: this deployment does not claim
  enterprise scope.
- **B6** is reported **`not probed`**, with the reason. Its endpoint is deliberately outside the
  spec's endpoint table and therefore outside the client. What the row reports instead is whether
  the fallback that replaces it is actually in place — how many `DEVIN_PLAYBOOK_IDS` entries are
  configured — because that, not the endpoint, is what the demo depends on.
- **B7** is reported as `registered`, not as *enforced*. The vocabulary was accepted; whether Devin
  rejects an unregistered tag is only observable at session creation, which this script does not
  do. B7's own verification step says as much.

A probe that raises an unexpected error — a `500`, a dead connection — is reported the same way as
a refusal. As far as the demo is concerned the consequence is identical: the panel uses the
fallback.

## Alternatives considered

| Option | Why not |
|---|---|
| Fail the run when an optional capability is refused | A refusal is the *expected* answer for an organisation-scoped token, which is the deployment the whole degradation design targets. It would make a successful bootstrap impossible for the configuration we most likely have |
| Probe B6 by creating a playbook | It leaves a playbook in the organisation to be cleaned up, and reaches an endpoint no other part of Sentinel can reach — a capability proven by the probe alone would still be unusable by the pipeline |
| Probe B7 by creating a session with a deliberately unregistered tag | It spends ACUs and leaves a session behind on every bootstrap. B7's verification is a one-off live experiment, not a step of a routine that runs whenever the vocabulary changes |
| Say nothing and let the operator read the worker's logs | The point of the sentence in the spec is that the degradation path is known *before* the demo. A warning line inside a session's logs is found afterwards |
| Report only the failures | An operator cannot tell "reachable" from "not probed this time" without the reachable rows, and the reachable case is the one that changes what the panels claim |

## Consequences

Exit status 0 does not mean every capability is present. It means the four required steps
succeeded; what is available has to be read off the table. The table is written to stdout while the
client's own structured logs go to stderr, so `make bootstrap-devin > bootstrap.txt` captures the
answer and nothing else.

B5, B6 and B7 stay open in `docs/blockers.md` until someone runs this against real credentials —
this decision is what makes that run produce a recorded fact rather than a shrug.

**What would tell us this was wrong:** an operator reading a green bootstrap as "enterprise scope
confirmed". If that happens the answer is a louder table — not a failed exit code, which would only
mean nobody can bootstrap at all.
