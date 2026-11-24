# SPDX-License-Identifier: Apache-2.0
"""The sandbox on the fixture agents: what the product's `mendari sandbox` commands do for the
bundled demo agent (portal messages), done here for the agents the tests keep, so that the kept
standards (SMART, Bulk, CDS Hooks with standard hooks, Da Vinci) are still exercised end to end.

- `seed_bundle`, `seed`: a fixture agent's local-site cases as US Core resources (the inbox's
  messages by the product's own builder, the others by `tests/support/fhir_records.py`), or
  the vendor-shaped EHR profile (`tests/support/ehr_profile.py`, LP-23e) under its own ids.
- `hook_request`, `expected_unread`, `profile_calls`, `run_profile`: the product's signed CDS
  client (`mendari.sandbox.cds_client`) on the EHR profile: discovery, then each standard hook
  (`patient-view` and `order-sign`) of each service the profile has patients for, once per
  patient, with no prefetch. An order hook's `draftOrders` are the patient's orders, in draft.
  A card may say it left a patient's note unread when its Binary is RTF, which no agent reads
  as text (`notes: unsupported_type`), and nothing else.

CI's `sandbox-e2e` runs both in the engine's image, with this folder mounted
(sandbox/fixtures.compose.yml):

    python -m tests.support.sandbox seed --fhir <FHIR base> --agent <a fixture agent's folder>
    python -m tests.support.sandbox seed --fhir <FHIR base> --ehr-profile
    python -m tests.support.sandbox cds-client --engine <URL> --fhir <FHIR base>

Nothing here imports pytest: the image holds the product alone.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from typing import Any

from mendari.core.registry import resolve_pack
from mendari.errors import MendariError
from mendari.sandbox import example_datasets_dir, fhir_workflow
from mendari.sandbox.cds_client import HookCall, Http, SandboxClient, _client
from mendari.sandbox.cds_client import hook_request as request_for
from mendari.standards.cds_hooks import ORDER_HOOKS
from tests.support import ehr_profile
from tests.support.ehr_profile import PRESCRIBER_ID, ProfilePatient
from tests.support.fhir_records import prior_auth_entries, referral_entries, trial_entries

INBOX = "mendari/patient-inbox-router"
REFERRAL = ehr_profile.REFERRAL_PACK
TRIAL = ehr_profile.TRIAL
PRIOR_AUTH = ehr_profile.PRIOR_AUTH
EHR_PROFILE = "ehr-profile"  # the vendor-shaped EHR profile (ehr_profile.py), not an agent
SEEDS = (INBOX, REFERRAL, TRIAL, PRIOR_AUTH, EHR_PROFILE)
# Each fixture agent's local-site example (in `tests/fixtures/datasets`), which the sandbox
# FHIR server holds as the "local site".
LOCAL_SITE = {
    INBOX: "local-site-example.jsonl",
    REFERRAL: "referral-local-site-example.jsonl",
    TRIAL: "trial-local-site-example.jsonl",
    PRIOR_AUTH: "prior-auth-local-site-example.jsonl",
}
# What `seed` counts of each: the resource a case or patient is about.
_FOCUS = {
    INBOX: "Communication",
    REFERRAL: "ServiceRequest",
    TRIAL: "Patient",
    PRIOR_AUTH: "ServiceRequest",
    EHR_PROFILE: "Patient",
}
_BUILDERS = {REFERRAL: referral_entries, TRIAL: trial_entries, PRIOR_AUTH: prior_auth_entries}
STANDARD_HOOKS = ("patient-view", "order-sign")
# What a hook may say it could not read on the profile: a note whose linked Binary is RTF, which
# no agent reads as text (the hooks read text/plain and text/html Binaries since LP-23h, and the
# fixture agents read their notes so since LP-23k).
RTF_NOTE = ("notes", "unsupported_type")
UNREADABLE_NOTE_TYPES = ("text/rtf",)
UNREAD_RULE = "only an RTF note may be unread"


# -- seeding ------------------------------------------------------------------------------------


def local_site(pack_id: str) -> str:
    """The agent's local-site example, as a dataset `mendari sandbox` commands take
    (`--dataset`): the file when the example datasets are installed, else the agent's own test
    set."""
    datasets = example_datasets_dir()
    local = datasets / LOCAL_SITE[pack_id] if datasets else None
    return str(local) if local is not None and local.is_file() else "test"


def seed_bundle(pack_id: str, count: int) -> dict[str, Any]:
    """The transaction `seed` posts: `count` local-site cases of the agent as FHIR resources, or
    (`EHR_PROFILE`) the whole vendor-shaped profile, which PUTs each resource under its id."""
    if pack_id == EHR_PROFILE:
        return ehr_profile.bundle()
    if pack_id == INBOX:  # portal messages: the product's own builder, and its mix of cases
        return fhir_workflow.seed_bundle(resolve_pack(INBOX), count, local_site(INBOX))
    if pack_id not in _BUILDERS:
        raise MendariError(f"the sandbox can seed {', '.join(SEEDS)}, not {pack_id}")
    cases = fhir_workflow.labelled_cases(resolve_pack(pack_id), local_site(pack_id)).records
    entries = [e for record in cases[:count] for e in _BUILDERS[pack_id](record)]
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


def seed(fhir: str, *, count: int, wait_seconds: int = 180, pack_id: str = INBOX) -> int:
    """Load `count` synthetic cases of the agent into the FHIR server, once (a server that
    already holds them is left alone), or the EHR profile, whose PUTs replace what an earlier
    seed of it made. Returns how many of the cases' focus resources (or the profile's Patients)
    it sent."""
    bundle = seed_bundle(pack_id, count)
    if pack_id == EHR_PROFILE:
        fhir_workflow.wait_for(fhir.rstrip("/"), wait_seconds)
        fhir_workflow.post(fhir, bundle)
    elif not fhir_workflow.post_once(fhir, bundle, wait_seconds=wait_seconds, named=pack_id):
        return 0
    return sum(e["resource"]["resourceType"] == _FOCUS[pack_id] for e in bundle["entry"])


# -- the signed CDS client on the EHR profile ----------------------------------------------------


def expected_unread(patient: ProfilePatient) -> set[tuple[str, str]]:
    """The keys and reasons a hook may say it could not read for the patient (an unreadable
    ABSTAIN's card): its note, when the Binary it links to is RTF; nothing for a patient with no
    note or a readable one."""
    binaries = {f"Binary/{r['id']}": r for r in patient.resources if r["resourceType"] == "Binary"}
    for document in (r for r in patient.resources if r["resourceType"] == "DocumentReference"):
        for content in document.get("content", []):
            binary = binaries.get(content.get("attachment", {}).get("url"), {})
            if binary.get("contentType") in UNREADABLE_NOTE_TYPES:
                return {RTF_NOTE}
    return set()


def profile_call(service: str, hook: str, patient: ProfilePatient) -> HookCall:
    """The call of a service's hook for one patient of the profile: the context an EHR sends
    (the user and, for an order hook, the patient's orders in draft), and what may be unread."""
    context: dict[str, Any] = {"userId": f"Practitioner/{PRESCRIBER_ID}"}
    if hook in ORDER_HOOKS:
        drafts = [{"resource": {**order, "status": "draft"}} for order in patient.orders()]
        context["draftOrders"] = {"resourceType": "Bundle", "type": "collection", "entry": drafts}
    return HookCall(
        service=service,
        patient=patient.id,
        context=context,
        hooks=STANDARD_HOOKS,
        unread=frozenset(expected_unread(patient)),
        unread_rule=UNREAD_RULE,
    )


def hook_request(hook: str, patient: ProfilePatient, fhir: str) -> dict[str, Any]:
    """A CDS Hooks request for one patient of the profile, as an EHR sends it with no prefetch:
    the context (the user, the patient and, for an order hook, the patient's orders in draft)
    and the FHIR server to read the record from."""
    service = patient.pack_id.replace("/", ".")
    return request_for(hook, profile_call(service, hook, patient), fhir)


def profile_calls(profile: list[ProfilePatient]) -> list[HookCall]:
    """One call for each patient of the profile, in the profile's order, of its agent's service.
    The agent's own manifest says whether its hook is an order hook; the client then holds
    discovery to a standard hook for the service."""
    calls = []
    for spec in ehr_profile.PACKS:
        service = spec.pack_id.replace("/", ".")
        declared = resolve_pack(spec.pack_id).manifest.cds_hooks
        hook = declared.hook if declared is not None else ""
        calls += [
            profile_call(service, hook, patient)
            for patient in profile
            if patient.pack_id == spec.pack_id
        ]
    return calls


def run_profile(
    client: SandboxClient,
    *,
    engine: str,
    fhir: str,
    audience: str | None = None,
    today: date | None = None,
    http: Http | None = None,
    fhir_http: Http | None = None,
) -> dict[str, Any]:
    """Discovery and every standard hook of the profile's agents at `engine`, with `fhir` as the
    FHIR server: what the client's `run` answers, after checking that the server holds the
    profile's patients (a server that cannot be reached raises)."""
    fhir = fhir.rstrip("/")
    profile = ehr_profile.patients(today)
    with _client(fhir_http) as reader:
        missing = [p.id for p in profile if not _held(reader, fhir, p)]
    if missing:
        return {
            "discovery": None,
            "calls": [],
            "jwks_fetches": 0,
            "problems": [
                f"{fhir} does not hold the EHR profile's patients ({', '.join(missing)}): "
                "seed it first (python -m tests.support.sandbox seed --ehr-profile)"
            ],
        }
    return client.run(
        engine=engine, fhir=fhir, calls=profile_calls(profile), audience=audience, http=http
    )


def _held(reader: Http, fhir: str, patient: ProfilePatient) -> bool:
    """Whether the FHIR server holds the patient."""
    found = reader.get(
        f"{fhir}/Patient/{patient.id}",
        headers={"accept": "application/fhir+json", "cache-control": "no-cache"},
    )
    return found.status_code == 200


# -- the command line (CI's `sandbox-e2e`, in the engine's image) -------------------------------


def _listen(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host.strip("[]"), int(port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    seeding = commands.add_parser("seed", help="load a fixture agent's cases, or the EHR profile")
    seeding.add_argument("--fhir", required=True, help="FHIR R4 base URL")
    which = seeding.add_mutually_exclusive_group(required=True)
    which.add_argument("--agent", help="a fixture agent, by its folder or its id")
    which.add_argument("--ehr-profile", action="store_true", help="the vendor-shaped profile")
    seeding.add_argument("--count", type=int, default=40)
    seeding.add_argument("--wait", type=int, default=180, help="seconds to wait for the server")
    calling = commands.add_parser(
        "cds-client", help="the signed CDS client on the EHR profile's patients"
    )
    calling.add_argument("--engine", required=True, help="the engine's URL, to call")
    calling.add_argument("--fhir", required=True, help="the FHIR server holding the EHR profile")
    calling.add_argument("--audience", help="the engine's MENDARI_PUBLIC_URL (default: --engine)")
    calling.add_argument("--listen", type=_listen, default=("127.0.0.1", 8300))
    calling.add_argument("--iss", help="the client's URL as the engine reaches it")
    calling.add_argument("--id", default="sandbox-client", help="its id in the registry")
    calling.add_argument("--org", default="default", help="its organization in the registry")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "seed":
            pack_id = EHR_PROFILE if args.ehr_profile else resolve_pack(args.agent).id
            created = seed(args.fhir, count=args.count, wait_seconds=args.wait, pack_id=pack_id)
            print(f"seeded {created} synthetic {pack_id} cases into {args.fhir}")
            return 0
        with SandboxClient(listen=args.listen, iss=args.iss) as client:
            entry = client.entry(client_id=args.id, org=args.org, fhir_server=args.fhir)
            print(f"MENDARI_CDS_CLIENTS entry: {json.dumps(entry)}", flush=True)
            result = run_profile(client, engine=args.engine, fhir=args.fhir, audience=args.audience)
    except MendariError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    for problem in result["problems"]:
        print(f"cds-client: {problem}", file=sys.stderr)
    return 1 if result["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
