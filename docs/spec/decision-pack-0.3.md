# Decision Pack Specification, version 0.3

Status: draft. Supersedes [0.2](decision-pack-0.2.md) for the FHIR mapping only. Every other
part of a pack is as 0.2 says, and a 0.3 manifest is a 0.2 manifest with `spec_version: "0.3"`.
Every valid 0.1 and 0.2 pack is still valid, with unchanged behaviour (§3). The key words MUST,
SHOULD and MAY are used as in RFC 2119. The reference implementation of the mapping is
`mendari.standards.fhir`.

## 1. What changes

Only the mapping part of 0.2 §9 ("FHIR and CDS Hooks"): how `fhir/mapping.yaml` turns FHIR R4
resources into a pack's input. §2 below states the whole mapping, its 0.2 keys and the new
ones. CDS Hooks, card and Task templates, the Task a decision becomes and the guard (§9.1)
stay as 0.2 says.

Real EHR data rarely puts a pack's code first, writes one resource type for one idea, or keeps
a note as plain text. 0.3 adds, each marked (0.3) below:

- a predicate that picks a coding or an identifier by its code system, wherever it is;
- `paths`: fallback paths, tried in order;
- `follow`: one reference hop, resolved inside the record and never fetched;
- the `attachment_text` transform: plain-text and HTML notes, inline or in a Binary;
- `code_sets` and the `code_set` selector: the pack's own codes, which a site's codes may be
  translated into;
- list `resources`: several resource types into one list, each with its own fields, and the
  `medication_period` derivation for a MedicationRequest.

## 2. The FHIR mapping

```yaml
fhir_version: R4
focus: Patient                    # the resource type a decision is about
focus_where: {path: ..., in: [...]}   # optional: which resources of that type count
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
reference reaches (`Medication.code.text`). `focus_where.path` is relative to the focus. In a
0.3 pack every path is checked when the mapping loads.

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
the next; once one gives a value, the others are not read. A field that gets no value is left
out of the input.

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
not followed further. A field's reference is resolved once, whatever number of paths it tries,
and a runtime SHOULD resolve it by a lookup rather than a search of the record, so that mapping
a record takes time in proportion to its size. A runtime MAY add resources it read itself to the
record before the mapping runs; the mapping still never fetches.

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
| `unsupported_type` | Any other content type (RTF, PDF, CDA), or none |
| `undecodable` | `data` that is not base64, or not UTF-8 once decoded |
| `not_fetched` | Only a `url`, to a Binary the record does not hold |

A runtime that records what it could not read (a data-coverage record) uses these reasons.

### 2.5 Code sets (0.3)

`code_sets` names sets of the pack's own codes: `{<name>: {system, codes}}`. A name is lower
case (`^[a-z][a-z0-9_]{0,63}$`). The system MUST be one the pack declares in `code_systems`,
and a code is listed once.

A field with `code_set: <name>` reads Codings, or CodeableConcepts whose codings it reads. Its
value is the first coding of the set's system whose code the set lists; another code of that
system, or the same code in another system, gives nothing. A `map` may then turn the code into
the pack's value.

A runtime MAY give the selector a site's translations into the set: `{system, code, as}`,
where `as` is one of the set's codes. A translation counts only into a code the set lists, and
only for its own set, and a coding of the set's own system wins over a translated one wherever
it is. Translations are configuration, not part of the pack, so they change no digest. The
reference runtime passes none yet.

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
writes when the drug is taken into two item keys, which the source's `fields` do not also map:

- the start is the first dosage instruction's `timing.repeat.boundsPeriod.start`, else
  `authoredOn`;
- the end is that `boundsPeriod.end`, else unknown (the key is left out). It is never inferred
  from the request's status, whether `completed` or `stopped`;
- a request whose status is `entered-in-error` is left out of the list.

`dispenseRequest.validityPeriod` is the window in which the drug may be dispensed, not when
the patient takes it, so it is not read. This rule is a clinical judgement: a pack that adopts
it SHOULD say so in its card's known failure modes, and a clinician should review it before
real use.

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
`FhirAdapter.looked_for`, for the data-coverage record.

## 3. Versions

A pack that uses any key marked (0.3) MUST declare `spec_version: "0.3"`. A 0.1 or 0.2 pack with
one is invalid, whatever the key holds (`code_sets: {}` too), and the problem names each key and
says to set `spec_version: '0.3'`. It is refused at load, wherever it comes from (an owned pack
too), and so by `mendari validate`.

An engine that reads only 0.1 and 0.2 refuses a 0.3 pack, since its `spec_version` is not one
it knows. Publish a pack as 0.3 only for engines that read 0.3.

Every 0.1 and 0.2 pack loads and maps as before. The reference packs' FHIR decisions are pinned
case by case, input hash included (`tests/fixtures/fhir/decisions-golden.json`), and the same
mappings written with `paths` and `resources` read every case alike.

## 4. Changes from 0.2 and migration

| Change | Section |
|---|---|
| `[system=<uri>]` in a path, for a uri `code_systems` declares | §2.1 |
| A 0.3 pack's paths are checked at load | §2.1 |
| `paths`, tried in order, the first that gives a value winning | §2.2 |
| `follow`: one reference hop, inside the record, never fetched | §2.3 |
| The `attachment_text` transform, and its three reasons | §2.4 |
| `code_sets` and the `code_set` selector, which a site's translations may feed | §2.5 |
| List `resources`: several resource types into one list | §2.6 |
| `medication_period` for a MedicationRequest | §2.7 |
| Bounds for a pack the deployment does not own: 4 paths a field, 200 codes a code set | §2.8 |

To migrate a pack:

1. Set `spec_version: "0.3"`.
2. Declare in `code_systems` every system a `[system=...]` or a code set names.
3. Change the paths that need it. A `path` that reads well stays as it is.
4. Re-run the evaluation. A mapping change changes the pack's digest (0.2 §11), so a
   governance approval of the old digest no longer applies, and it may change what the pack
   decides through FHIR.
