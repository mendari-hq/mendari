# Decision Pack Specification, version 0.3

Status: draft. Supersedes [0.2](decision-pack-0.2.md) for the FHIR mapping and one key of the
`criteria` provider's medication check only. Every other part of a pack is as 0.2 says, and a 0.3
manifest is a 0.2 manifest with `spec_version: "0.3"`. Every valid 0.1 and 0.2 pack is still
valid, with unchanged behaviour (§4). The key words MUST,
SHOULD and MAY are used as in RFC 2119. The reference implementation of the mapping is
`mendari.standards.fhir`.

## 1. What changes

The mapping part of 0.2 §9 ("FHIR and CDS Hooks"): how `fhir/mapping.yaml` turns FHIR R4
resources into a pack's input. §2 below states the whole mapping, its 0.2 keys and the new
ones. CDS Hooks, card and Task templates, the Task a decision becomes and the guard (§9.1)
stay as 0.2 says. And one key of the `criteria` provider (0.2 §5), `current_within_days` on a
medication check (§3).

Real EHR data rarely puts a pack's code first, writes one resource type for one idea, or keeps
a note as plain text. 0.3 adds, each marked (0.3) below:

- a predicate that picks a coding or an identifier by its code system, wherever it is;
- `paths`: fallback paths, tried in order;
- `follow`: one reference hop, resolved inside the record and never fetched by the mapping (a
  runtime may first read what the record lacks: links, §2.3);
- the `attachment_text` transform: plain-text and HTML notes, inline or in a Binary;
- `code_sets` and the `code_set` selector: the pack's own codes, which a site's codes may be
  translated into, for a field and for `focus_where`;
- list `resources`: several resource types into one list, each with its own fields, and the
  `medication_period` derivation for a MedicationRequest;
- `current_within_days` on a `criteria` medication check: an active medication with no end
  date is current only when it started within that window (§3).

## 2. The FHIR mapping

```yaml
fhir_version: R4
focus: Patient                    # the resource type a decision is about
focus_where: {path: ..., in: [...]}   # optional: which resources of that type count
                                  # (0.3: or {path: ..., code_set: <name>}, §2.5)
resources:                        # the types the record may hold; `required` ones must be there
  Patient: {required: true}
code_sets:                        # (0.3) optional
  t2d: {system: http://hl7.org/fhir/sid/icd-10-cm, codes: [E11.9, E11.65]}
fields:                           # input field -> one value
  patient.age: {path: Patient.birthDate, transform: age_years}
  notes: {path: DocumentReference.content.attachment, transform: attachment_text}   # (0.3)
lists:                            # input field -> a list of objects
  conditions:
    resource: Condition
    fields:
      code: {path: code.coding, code_set: t2d}                                      # (0.3)
      text: {path: code.text}
  medications:
    resources:                                                                      # (0.3)
      MedicationStatement:
        fields:
          code: {path: "medicationCodeableConcept.coding[system=http://www.nlm.nih.gov/research/umls/rxnorm].code"}
          start: {path: effectivePeriod.start}
          end: {path: effectivePeriod.end}
      MedicationRequest:
        fields:
          code:
            follow: medicationReference
            paths:
              - "Medication.code.coding[system=http://www.nlm.nih.gov/research/umls/rxnorm].code"
              - Medication.code.text
        medication_period: {start: start, end: end}
task: {code_text: ..., performer_system: ..., performer: ..., description: ...}
```

The input MAY be a Bundle, a single resource, or a CDS Hooks prefetch object whose values are
resources or search Bundles. All are flattened, and resources are de-duplicated by type and id.
Only the focus must be unique. The Task's `description` and `performer` are templates (0.2 §9).

### 2.1 Paths

A path is a small FHIRPath subset: dotted element names. A list-valued element yields all its
items, and `[n]` keeps the n-th of everything the path found so far (`[*]` keeps all). (0.3)
`[system=<uri>]` keeps the items whose `system` is that uri: a coding of one code system
(`code.coding[system=http://loinc.org].code`), or an identifier (`identifier[system=...]`).
The uri MUST be one the pack declares in `code_systems` (0.2 §3), so a licensed system is
declared with its terms.

A top-level field's path starts with a resource type (`Patient.birthDate`). A list field's
path is relative to its resource (`code.text`). After a `follow`, a path starts with the type the
reference reaches (`Medication.code.text`), or with the field's own type (§2.3).
`focus_where.path` is relative to the focus. In a 0.3 pack every path is checked when the
mapping loads.

### 2.2 Fields

A field gives one value. It has a `path` or (0.3) `paths`, not both, and optionally:

- `transform`: `join` (every text value, one per line), `age_band`, `age_years` (from a FHIR
  date), `base64_text` (every `data`, decoded and joined, whatever its content type) or (0.3)
  `attachment_text` (§2.4).
- `map`: what was found, to the pack's value. A value the map does not list gives nothing.
- (0.3) `code_set`: §2.5. It takes no `transform`.
- (0.3) `follow`: §2.3.

With `paths`, each path is tried in order, and the first that gives a value is the field's. A
path that finds nothing, or whose value the transform or the map turns into nothing, passes to
the next; once one gives a value, the others are not read (a value through a site's
translation is the one exception, §2.5). A field that gets no value is left out of the input.

### 2.3 Following a reference (0.3)

`follow` is a path to a Reference: from a resource type in a top-level field
(`Coverage.payor`), relative to the resource in a list field (`medicationReference`). The
reference is resolved inside the record only, and never fetched:

- `#<id>`: a contained resource of the resource that holds the reference;
- the exact `fullUrl` of a Bundle entry (`urn:uuid:...`, or an absolute URL an entry has);
- `<Type>/<id>`, optionally with `/_history/<version>`: that resource among the record's.

Anything else resolves to nothing: an absolute URL no entry has, a reference by identifier
only, or a resource of another type than the field's paths start with. The field's paths are
then read in the resources the reference reached. A `follow` is one hop: what it reaches is
not followed further.

A path that starts with the field's **own type** is read where the field is, as it would be
without `follow`: a list field's resource type, or a top-level field's `follow` path's type
(`Coverage` for `Coverage.payor[0]`). So a field may fall back, in its `paths`' order, from what
the reference reaches to what its own resource says, for an element R4 lets be either a
reference or a value (a MedicationRequest's `medicationReference` or `medicationCodeableConcept`)
or a reference that may carry only an identifier:

```yaml
coverage.payer:
  follow: Coverage.payor[0]
  paths:
    - "Organization.identifier[system=https://payers.example/id].value"   # what it reaches
    - Coverage.payor[0].identifier.value                                  # its own resource
```

A field therefore never reads, through `follow`, a resource of its own type: a reference from a
resource to another of its type is not followed for its paths. A field's reference is resolved once, whatever number of paths it tries,
and a runtime SHOULD resolve it by a lookup rather than a search of the record, so that mapping
a record takes time in proportion to its size. A runtime MAY add resources it read itself to the
record before the mapping runs; the mapping still never fetches.

A reference that reaches nothing is a **link** when it is `<Type>/<id>` (relative, or at the end
of an absolute URL with no query) of a type the field's paths read through it (not the field's
own type). `#<id>`, `urn:...`, a
reference by identifier only, and a reference to another type (a Coverage's payor that is the
patient, for a field that reads an Organization) are not links: they reach nothing, and are not
unreadable. A runtime that records what it could not read counts a link the record does not
hold by why it was not read (§2.4's reasons, and those of a read that failed), else
`not_fetched`.

**Linked reads (the reference runtime).** On a CDS Hooks request with a `fhirServer`, and in the
SMART launch's decide, the reference runtime reads the links a 0.3 mapping reaches that the
record does not hold, a note's Binary included (§2.4), and adds what it read to the record
before the mapping runs. Each is one read of one resource, of the type the field reads, from that
server only, under its base URL and through its allow-list (a link anywhere else is
`off_server`, never read; a request whose server is off the allow-list is refused before anything
is decided, as its prefetch read is). A decision reads at most 20 links (past them, `cap`), each
at most 1 MiB once decoded (`too_large`), and a link is read once a request whatever its
spelling. A resource its list leaves out (a MedicationRequest entered in error, §2.7) is not an
item: nothing it links to is read, or counted. A read that fails gives its reason (`timeout`,
`unauthorized`, `not_r4` and the others). A link that is not read makes the decision ABSTAIN with
`could not read <field>: <reason>`, whatever a later path of the field reads in its own resource. `fhir/decide` and `$decide` never read a link: there it is
`not_fetched`, and the decision abstains.

### 2.4 Attachment text (0.3)

`attachment_text` reads the path's values as Attachments
(`DocumentReference.content.attachment`). An attachment's content is its inline `data` or, with
only a `url`, a Binary the url names inside the record (resolved as in §2.3). `text/plain` is
read as it is. `text/html` is read as text: scripts and styles dropped, entities decoded, each
block on its own line. Both are read as UTF-8, and a content type's parameters
(`; charset=utf-8`) are ignored. `data` is base64 as R4 has it: spaces and line breaks between
its characters (data wrapped every 76 characters) are allowed. The texts are joined with a blank
line between them.

An attachment that cannot be read adds no text, and the reason is one of three:

| Reason | When |
|---|---|
| `unsupported_type` | Any other content type (RTF, PDF, CDA), or none; a `url` to a resource of another type than a Binary, or to a Binary with no content |
| `undecodable` | `data` that is not base64, or not UTF-8 once decoded |
| `not_fetched` | Only a `url`, to a Binary the record does not hold |

A runtime that reads links (§2.3) gives, for a url it did not read, why: `off_server`, `cap`,
`too_large`, or a failed read's reason. It reads a Binary as FHIR JSON, and only a url that is
`Binary/<id>` (relative, or under the server's base); another url under the server is
`not_fetched`, and one that names another type (relative or absolute) `unsupported_type`, never
read. Its content type is checked first, so an RTF or PDF attachment's url is never read.

A runtime that records what it could not read (a data-coverage record) uses these reasons. The
reference runtime counts each in the decision's data-coverage record and, as for a 0.2
`base64_text` note it cannot read (0.2 §9), makes the decision ABSTAIN, with the reason
`could not read <field>: <reason>`. An attachment with neither `data` nor a `url` has nothing to
read, and counts for nothing.

**Longer than the input allows.** The texts of all of a record's notes, joined, can be longer
than the `maxLength` the pack's input schema gives the field. The reference runtime never cuts
the text (a cut could lose the very sentence a question reads) and does not let the schema
refuse the whole input for it: it leaves the field out of the input and makes the decision
ABSTAIN with `could not read <field>: too_large`, counted once in the data-coverage record.
This applies to a top-level field (`fields`) whose schema property, found through `properties`,
has a `maxLength`.

**Renditions.** R4 makes the repetitions of a DocumentReference's `content` one document in
different formats, and a DiagnosticReport's `presentedForm` semantically equivalent forms of one
report. Where a path reads either, an attachment that cannot be read counts for nothing when
another attachment of the same resource was read as text: an RTF beside the HTML of one note
does not make the decision abstain, and a runtime that reads links reads no other rendition
once one reads. A note whose only renditions cannot be read still counts, and abstains, and so
does any other attachment that cannot be read (two payloads of a Communication are two
contents).

### 2.5 Code sets (0.3)

`code_sets` names sets of the pack's own codes: `{<name>: {system, codes}}`. A name is lower
case (`^[a-z][a-z0-9_]{0,63}$`). The system MUST be one the pack declares in `code_systems`,
and a code is listed once.

A field with `code_set: <name>` reads Codings, or CodeableConcepts whose codings it reads. Its
value is the first coding of the set's system whose code the set lists; another code of that
system, or the same code in another system, gives nothing. A `map` may then turn the code into
the pack's value.

`focus_where` MAY name a code set (`{path, code_set}`) in place of `in`: a resource of the focus
type counts when the codings at its path, read as a field's are, give one of the set's codes
(`category.coding` for a referral's category, wherever the pack's coding sits among the
categories' codings). The set's translations count as a field's do, so a site's own code for
the focus finds it.

A runtime MAY give the selector a site's translations into the set: `{system, code, as}`,
where `as` is one of the set's codes. A translation counts only into a code the set lists, and
only for its own set, and a coding of the set's own system wins over a translated one wherever
it is: among the codings one path finds, and across a field's `paths`. A path that gives a
code only through a translation passes to the next, for one of the set's own codes; when no
path gives one, the first translated code is the field's. A record whose own codes give a
field its value is so read the same with translations as without. Translations are
configuration, not part of the pack, so they change no digest. The reference runtime's server
reads them from `MENDARI_SITE_CODES`, one map per organization, checked at startup against the
code sets the packs declare (its command line reads none). A decision records the version and
SHA-256 of the map it read with, and one whose input took a value through a translation, or
whose focus counted only through one, is never AUTO, nor BLOCKed by a rule without a person
([Connecting an EHR](../guides/connecting-an-ehr.md#site-codes)).

### 2.6 Lists

A list gives a list of objects. It has `resource` and `fields` (one object per resource of that
type, its fields' paths relative to it), or (0.3) `resources`, several types into one list:

```yaml
resources:
  <Type>: {fields: {...}, medication_period: {start: <key>, end: <key>}}
```

Items come in the order the types are given, then the record's order. A resource whose fields
all give nothing is left out.

### 2.7 A MedicationRequest's period (0.3)

`medication_period: {start: <key>, end: <key>}`, under `resources.MedicationRequest` only,
writes when the drug is taken into two item keys, which the source's `fields` do not also map.
Only the first dosage instruction (`dosageInstruction[0]`) is read, and of its timing only
`repeat.boundsPeriod` and `repeat.boundsDuration`:

- the start is that `boundsPeriod.start`, else `authoredOn`, as written;
- the end is that `boundsPeriod.end`, as written. Else, when the start is a date or a dateTime
  with its day (a year or a month alone is not) and `boundsDuration` gives a number of days,
  the end is the start's day plus that many days, as a date (`YYYY-MM-DD`). Else the end is
  unknown (the key is left out). It is never inferred from the request's status, whether
  `completed` or `stopped`;
- a `boundsDuration` gives a number of days when its `value` is a number of at least 0, its
  `code` is one of the UCUM units below and its `system`, if any, is UCUM's
  (`http://unitsofmeasure.org`). The days are `value` times the unit's days, rounded up to a
  whole day, so the end is never earlier than the duration says. A duration in another unit, or
  given only as text (`unit`), gives none;

  | `code` | Unit | Days |
  |---|---|---|
  | `h` | hour | 1/24 |
  | `d` | day | 1 |
  | `wk` | week | 7 |
  | `mo` | month (UCUM's mean Julian month) | 30.4375 |
  | `a` | year (UCUM's Julian year) | 365.25 |

- a request is left out of the list when its `status` is `entered-in-error`, when its
  `intent` is `proposal`, `plan` or `option` (not an order to give the drug), or when its
  `doNotPerform` is `true` (an order not to give it). Every other request is listed, whatever
  its status, as its own item: requests for one drug, a renewal and the order it renews, are
  not joined.

`dispenseRequest.validityPeriod` is the window in which the drug may be dispensed, not when
the patient takes it, so it is not read. `boundsRange` is not read either. This rule is a
clinical judgement: a pack that adopts it SHOULD say so in its card's known failure modes, and a
clinician should review it before real use.

### 2.8 Packs the deployment does not own

A pack the deployment does not own (a community pack, or one from a registry that is not a
reference pack of the release) is held to bounds at load: at most 4 paths a field, and at most
200 codes a code set. `mendari validate` holds every pack to them. A `follow` is one hop for
every pack.

### 2.9 What a field looks for

A runtime MAY report, for each field and list item key, what it looks for: its paths in the
order tried, the reference it follows, the code systems it selects on and the pack's own codes
it looks for (its code set's, and its map's keys). Such a report comes from the mapping alone,
so it holds no patient's value, id or text. The reference runtime's is
`FhirAdapter.looked_for`, and its data-coverage record (`mendari.data-coverage/1`) counts what
each field found against it: the values and codings at the paths it read (in order, until one
gave the field its value), and with `follow` the references it found, how many it reached and
each link it could not reach by its reason (§2.3). A resource a linked read added counts among
what a reference reached, never among the record's resources of its type. A list of several
types has a record for each type (`sources`), a `medication_period`'s two keys among its fields.

## 3. A current medication (0.3)

A `criteria` provider's medication check (0.2 §5) counts a medication as current when its
status is one of the check's `statuses` (by default `active`). A medication list rarely says when
a drug was stopped, so with `current_within_days: <days>` (an integer of at least 0), a
medication of one of those statuses that has no `end` counts as current only when its `start` is
at most that many days before the screening date:

```yaml
stable_metformin:
  checks:
    - medication: {names: ["metformin"], min_days: 90, current_within_days: 365, then: "yes"}
  default: "no"
```

When such a medication has no `start`, or one earlier than the window, the check answers
`unknown`, whatever else it would answer, so `unknown` MUST be one of the question's labels.
A start on the window's first day (exactly `<days>` days before) is within it; a start after the
screening date is not earlier than the window, so it is read as without the key. The rule reads
every item of the list alike, whatever resource it came from (a MedicationStatement's
`effectivePeriod`, a MedicationRequest's `medication_period`, §2.7). Without the key a check reads
as in 0.2. As in 0.2, the first item that applies decides: a medication not known to be
current answers `unknown`, even when a later item would have decided.

## 4. Versions

A pack that uses any key marked (0.3) MUST declare `spec_version: "0.3"`. A 0.1 or 0.2 pack with
one is invalid, whatever the key holds (`code_sets: {}` and `current_within_days: null` too), and
the problem names each key and says to set `spec_version: '0.3'`. It is refused at load,
wherever it comes from (an owned pack too), and so by `mendari validate`.

An engine that reads only 0.1 and 0.2 refuses a 0.3 pack, since its `spec_version` is not one
it knows. Publish a pack as 0.3 only for engines that read 0.3.

Every 0.1 and 0.2 pack loads and maps as before. The reference packs' FHIR decisions are pinned
case by case, input hash included (`tests/fixtures/fhir/decisions-golden.json`), and a 0.2
mapping written with `paths` and `resources` reads every case alike. Three reference packs are
0.3 packs themselves (the Clinical Trial Prescreener, the Prior Authorization Preflight and the
Referral Router, since their 0.3.0): their `fhir/mapping.yaml` files use most of this section.

## 5. Changes from 0.2 and migration

| Change | Section |
|---|---|
| `[system=<uri>]` in a path, for a uri `code_systems` declares | §2.1 |
| A 0.3 pack's paths are checked at load | §2.1 |
| `paths`, tried in order, the first that gives a value winning | §2.2 |
| `follow`: one reference hop, inside the record, never fetched by the mapping; a path of the field's own type read in place | §2.3 |
| Links: what a reference or a note's url reaches that the record does not hold, read by the reference runtime on a CDS hook or the SMART launch's decide | §2.3, §2.4 |
| The `attachment_text` transform, its three reasons, renditions, and notes longer than the input allows | §2.4 |
| `code_sets` and the `code_set` selector, for a field and for `focus_where`, which a site's translations may feed | §2.5 |
| List `resources`: several resource types into one list | §2.6 |
| `medication_period` for a MedicationRequest | §2.7 |
| `current_within_days` on a `criteria` medication check | §3 |
| Bounds for a pack the deployment does not own: 4 paths a field, 200 codes a code set | §2.8 |

To migrate a pack:

1. Set `spec_version: "0.3"`.
2. Declare in `code_systems` every system a `[system=...]` or a code set names.
3. Change the paths that need it. A `path` that reads well stays as it is.
4. Re-run the evaluation. A mapping change changes the pack's digest (0.2 §11), so a
   governance approval of the old digest no longer applies, and it may change what the pack
   decides through FHIR.
