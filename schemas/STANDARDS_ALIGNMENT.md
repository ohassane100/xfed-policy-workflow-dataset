# Readable policies and standards export

`05_policy_schema.json` defines the saved **v2** policy. It is an XFed application
format, not a claim of complete ODRL conformance or an executable legal opinion.
`05_odrl_schema.json` retains the previous compatibility format. Conversion is
deterministic in `xfed.policy`; extraction still uses a small source-backed plan
and the existing compiler/checks internally.

| Readable field | Meaning / compatibility mapping |
|---|---|
| `version`, `contract_id`, `id`, `type` | Format version, source contract, stable policy ID, Agreement or Set. An unknown issuer uses Set. |
| `rules[].id`, `kind` | Stable rule ID; permission, prohibition or obligation. |
| `actor`, `issuer`, `action`, `asset` | Responsible party, optional grantor, atomic action, affected information. Map to assignee, assigner, action and target. |
| `conditions` | AND by default. Each comparison has `field`, `op`, and either `value` or unresolved `parameter`; optional `type` and `unit`. |
| `all`, `any`, `exactly_one` | AND, OR, exclusive OR. Preserve logical scope; a prohibition applies when its conditions hold. |
| `requires`, `remedies` | Nested permission prerequisites and duties following prohibition breach; retain their own sources and conditions. |
| `source`, `context` | Primary source catalogue key and optional supporting keys. Context does not create independent primary rules. |
| `sources` | Exact quotations stored once, with chunk ID, optional clause number, physical PDF pages and block IDs. Different excerpts from one chunk have distinct keys. |
| `document.sha256` | Original PDF fingerprint when available. A hash identifies bytes; it does not prove semantic correctness. |
| `parameters`, `parties` | Explicit unresolved external values or party identities with evidence. They require resolution before enforcement. |
| `metadata.osdu` | Optional source-supported OSDU LegalTag / record-level legal metadata, separate from rule logic. |
| `export_context` | Optional preservation of non-default JSON-LD contexts/profile for compatibility export. Normally absent. |

Purpose, recipients, location, retention, triggers, deadlines, security, prices and
exceptions stay in scoped conditions rather than many separate mandatory fields.
An absent permission is not automatically a prohibition. A schema-valid citation
does not establish that the cited text entails the encoded rule.

The UI reads v2 and legacy files, shows each rule beside its primary text and
supporting context, and links to the physical source PDF page. **Export ODRL**
restores the existing ODRL/IDS representation and optional OSDU metadata. Empty
extractions have no ODRL policy to export. Source-backed custom labels/operands
still require deployment mapping; this project does not implement a general ODRL
enforcement engine. OSDU allowed values must be validated in the target partition.

References: [ODRL model](https://www.w3.org/TR/odrl-model/),
[OSDU Legal Service](https://osdu.pages.opengroup.org/platform/security-and-compliance/legal/api/).
