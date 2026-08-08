---
title: The bootstrap capability probe reports a refusal and fails on a fault
status: accepted
date: 2026-08-08
type: process
areas: [devin]
tasks: [T40]
files: [scripts/bootstrap_devin.py]
specs: [docs/09-operations.md]
supersedes:
---

# The bootstrap capability probe reports a refusal and fails on a fault

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

One line is already drawn, and it is not this script's to redraw. `client.py` maps only `403` and
`404` into `Unavailability` and raises everything else, `401` most pointedly: *"a rejected token is
a misconfiguration that must be fixed, and silently falling back to derived figures would hide
it."* Whatever reaches the probe's `except` is therefore something the client refused to call a
capability gap.

## Decision

**A refusal is reported and costs nothing; a fault is reported and costs the exit status.** Every
row states what was asked and what came back, and the four required steps are the only thing that
can stop the run before the table is printed.

Four statuses, and only one of them claims a fallback:

| Status | Means | Claims a fallback |
|---|---|---|
| `reachable` | Asked, answered | — |
| `degraded` | Asked, **refused** — the `403`/`404` of `client.DEGRADES` | Yes, quoted from `Capability.fallback` |
| `fault` | Asked, and something happened that is not an answer — `401`, `5xx`, a dead connection, a body that will not parse | **No** |
| `not probed` | Not asked, and why | — |

A `fault` leaves the capability an open question, so the run exits non-zero — *after* the whole
table has been printed, because the four steps did succeed and the rest of the answer is worth
having. Writing "use the derived figures" next to a `401` would answer a question nobody asked, and
would undo one layer up the distinction `client.py` draws one layer down.

Per blocker:

- **B5** and the ACU-spend row are probed for real, through `session_metrics()` and
  `daily_consumption()`. With `DEVIN_ENTERPRISE_ID` unset — which is how `.env.example` ships —
  nothing is asked, so the row is `not probed` and says B5 stays open. B5 asks whether the *token*
  carries `ViewAccountMetrics`; an unset id only says the panel is switched off, and `degraded`
  there would let an operator close B5 on a run that made no request.
- **B6** is reported **`not probed`**, with the reason. Its endpoint is deliberately outside the
  spec's endpoint table and therefore outside the client. What the row reports instead is whether
  the fallback that replaces it is actually in place — how many `DEVIN_PLAYBOOK_IDS` entries are
  configured — because that, not the endpoint, is what the demo depends on. Where B5 was not asked,
  B6 says so rather than pointing at a row that answered nothing.
- **B7** is reported as `registered`, not as *enforced*. The vocabulary was accepted; whether Devin
  rejects an unregistered tag is only observable at session creation, which this script does not
  do. B7's own verification step says as much.

The four steps remain fatal to each other — a failed tag registration stops the run before the
notes are created. That is not in tension with the above: they are what `docs/09-operations.md`
*requires* the script to do, and a half-registered organisation is a state to fix rather than a
capability to work around. Only the optional capabilities are reported and moved past.

## Alternatives considered

| Option | Why not |
|---|---|
| Fail the run when an optional capability is refused | A refusal is the *expected* answer for an organisation-scoped token, which is the deployment the whole degradation design targets. It would make a successful bootstrap impossible for the configuration we most likely have |
| Report a fault as one more `degraded` row | It reads as an answer. A `401` on the enterprise endpoint after step 1 accepted the token against the *organisation* endpoints is exactly the case B5 is about, and `401` versus `403` is the difference between "wrong token" and "no enterprise scope" — recorded as `degraded`, the operator writes down the fallback and closes a blocker that is still open |
| Report a fault but still exit 0 | The table is long and a run that ends green is read as a run that answered. The exit status is the one part of the output nobody skims |
| Probe B6 by creating a playbook | It leaves a playbook in the organisation to be cleaned up, and reaches an endpoint no other part of Sentinel can reach — a capability proven by the probe alone would still be unusable by the pipeline |
| Probe B7 by creating a session with a deliberately unregistered tag | It spends ACUs and leaves a session behind on every bootstrap. B7's verification is a one-off live experiment, not a step of a routine that runs whenever the vocabulary changes |
| Say nothing and let the operator read the worker's logs | The point of the sentence in the spec is that the degradation path is known *before* the demo. A warning line inside a session's logs is found afterwards |
| Report only the failures | An operator cannot tell "reachable" from "not probed this time" without the reachable rows, and the reachable case is the one that changes what the panels claim |

## Consequences

Exit status 0 does not mean every capability is present. It means the four required steps succeeded
and every optional capability was either answered or deliberately not asked; what is *available*
has to be read off the table. The table is written to stdout while the client's own structured logs
go to stderr, so `make bootstrap-devin > bootstrap.txt` captures the answer and nothing else.

A non-zero exit now has two meanings — a required step failed, or a capability could not be asked —
and they are told apart by whether the table was printed. A transient `5xx` on an optional endpoint
therefore fails a run whose four steps all succeeded; re-running costs nothing, because they are
idempotent, and the alternative is a fault that looks like an answer.

With the shipped `.env.example`, B5 and B6 both come back `not probed`: the default configuration
cannot answer them. That is the honest result, and the row says what to set to change it.

B5, B6 and B7 stay open in `docs/blockers.md` until someone runs this against real credentials —
this decision is what makes that run produce a recorded fact rather than a shrug.

**What would tell us this was wrong:** an operator reading a green bootstrap as "enterprise scope
confirmed" — the answer to which is a louder table, not a failed exit code. Or the mirror image:
a fault status that fires on something routine enough that operators start ignoring a red run,
at which point the line between refusal and fault is drawn in the wrong place rather than drawn
too sharply.
