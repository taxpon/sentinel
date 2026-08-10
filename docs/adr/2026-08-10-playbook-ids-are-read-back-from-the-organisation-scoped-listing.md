---
title: Playbook ids are read back from the organisation-scoped listing, not the enterprise one
status: accepted
date: 2026-08-10
type: process
areas: [devin, ops]
tasks: [T40]
files: [scripts/bootstrap_devin.py, src/sentinel/devin/client.py, src/sentinel/devin/schemas.py]
specs: [docs/05-devin-integration.md]
supersedes:
---

# Playbook ids are read back from the organisation-scoped listing, not the enterprise one

## Context

`DEVIN_PLAYBOOK_IDS` is required: without it `api`, `worker` and `poller` refuse to start, and every
session is created with one of its values. It maps playbook name — or issue class — onto the
`playbook_id` the create-session call takes.

The four playbooks are created by hand in the Devin UI ([B6](../blockers.md#b6)), which leaves the
operator holding four playbooks and no ids. Neither
[Creating playbooks](https://docs.devin.ai/product-guides/creating-playbooks) nor the create-session
reference says where a playbook id is surfaced or what shape it takes, so "look in the UI" is advice
nobody could follow reliably.

The v3 API turns out to expose playbooks under **two** scopes, which the endpoint list at
`https://docs.devin.ai/llms.txt` enumerates and the OpenAPI fragments on each page specify:

| | Listing | Permission the page states |
|---|---|---|
| Organisation | `GET /v3/organizations/{org_id}/playbooks` | `ManageAccountPlaybooks` for the specified organisation |
| Enterprise | `GET /v3/enterprise/playbooks` | `ManageAccountPlaybooks` at the enterprise level |

Both answer a `PaginatedResponse[PlaybookResponse]`: `items[]` of `playbook_id`, `title`, `body`,
`macro`, `access_type` (`enterprise` or `org`), `org_id`, timestamps, with `after` and `first`
(default 100, maximum 200) for paging. v1 and v2 have playbook endpoints of their own; they are
[not reachable from this client](./2026-08-07-devin-v3-only.md) and were not considered.

## Decision

`make devin-playbooks` — `scripts/bootstrap_devin.py --list-playbooks` — reads
`GET /v3/organizations/{org_id}/playbooks` and prints each playbook as title, id and `access_type`,
followed by the `DEVIN_PLAYBOOK_IDS` to paste into `.env`, matched by title against the four names in
[`docs/playbooks/`](../playbooks/README.md).

Three things follow from what it is for.

**Organisation-scoped, not enterprise-scoped.** The enterprise listing needs a scope B5 and B6
already record as unverified, and `DEVIN_ENTERPRISE_ID` is optional configuration a deployment may
not set. The organisation listing needs `DEVIN_ORG_ID`, which every deployment has by definition, and
its `access_type` field reports an enterprise-level playbook anyway — so the narrower call is also
the one that sees more of what matters here. The enterprise listing is not called at all: a second
endpoint reached only as a fallback would be a second row in the endpoint table and a second thing to
verify, for an organisation that has four playbooks.

**Read-only, and it does not write `.env`.** Every other step of this script records what it created;
this one prints. The ids are matched to names by *title*, which is a string somebody typed into a web
form — a rewrite of a file full of credentials, made from a match like that, is a write made on a
guess. Two playbooks sharing a title are reported and left out of the JSON rather than resolved
arbitrarily, because an id in `.env` that points at the wrong playbook is invisible: the class's
sessions run, under instructions nobody chose.

**A refusal is an answer.** `client.DEGRADES` already turns `403` and `404` into `Unavailable`, so a
service user without the playbook permission gets one line naming the capability and the fallback —
open each playbook in the web app and read its id from the page — and the command exits 0. A `401` or
a `500` is a fault, claims no fallback and exits non-zero, which is the distinction
[a refusal is reported, a fault is not](./2026-08-08-a-refusal-is-reported-a-fault-is-not.md) draws
for the probe.

`PLAYBOOK_DISCOVERY` therefore joins the degradation table in
[`05`](../05-devin-integration.md#degradation) as a fourth row rather than becoming a private
vocabulary in the script.

## Alternatives rejected

**Ask for the enterprise listing first and fall back to the organisation one.** Two endpoints, two
rows in the table, and a report that has to explain which one answered — to reach the same four ids.
The organisation listing already reports enterprise-level playbooks through `access_type`.

**Have the option write `DEVIN_PLAYBOOK_IDS` into `.env` itself.** It is the obvious next step and it
is the one thing the matching is not reliable enough to justify. See above.

**Walk the cursor to collect every page.** `after` and `first` are documented and paging is real, but
a deployment with four playbooks does not need a loop, and nothing else in Sentinel pages. One page is
requested and `has_next_page` is reported, which tells an operator the list is partial instead of
letting it look complete.

**Leave it to the UI and document where to click.** This is what the option replaces, and it is
exactly what the option falls back to when the permission is missing. As a *primary* answer it fails
because no Devin documentation states where the id appears, so the instruction could not be written.

## Consequences

- One row is added to the endpoint table in `05`: `GET /v3/organizations/{org_id}/playbooks`, the
  only read Sentinel makes that no runtime process makes.
- `--list-playbooks` loads a configuration that does **not** require `DEVIN_PLAYBOOK_IDS` — it is the
  map the option exists to fill in. The bootstrap run and the three services still require it.
- **Unverified** ([B8](../blockers.md#b8)): no credentials exist, so nothing here has been run against
  Devin. What is unconfirmed is whether the permission the organisation page names
  (`ManageAccountPlaybooks`) is the one it actually enforces — the permission table in the same
  reference assigns `ManageOrgPlaybooks` to "Playbooks (organization)", and the two pages disagree.
  Either way the outcome is one of the two this option already handles: a listing, or a `403` with the
  fallback.
