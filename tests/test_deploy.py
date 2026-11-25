# SPDX-License-Identifier: Apache-2.0
"""Per-pack images (MENDARI_AGENTS) and the templates in deploy/: the one-machine install
(`deploy/site`: its compose file, its front, its daily jobs and the script that drives them),
the on-prem template and Kubernetes."""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import install
from mendari.errors import MendariError
from mendari.server.app import create_app

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
SITE = DEPLOY / "site"
GUIDE = ROOT / "docs" / "guides" / "install-at-your-site.md"
# What an install made in this copy adds to `deploy/site` (its `.gitignore`): this machine's
# own, never a template, and not text (a certificate's key, database dumps).
INSTALLED = (".env", "tls", "backups")
posix = pytest.mark.skipif(os.name == "nt", reason="the template's scripts are POSIX shell")


def templates() -> list[Path]:
    """Every file of the templates in `deploy/`, without what an install made there."""
    found = []
    for path in sorted(DEPLOY.rglob("*")):
        inside = path.relative_to(DEPLOY).parts
        installed = (
            len(inside) > 1
            and inside[0] == "site"
            and (inside[1] in INSTALLED or inside[1].startswith(".env."))
        )
        if path.is_file() and not installed:
            found.append(path)
    return found


def test_a_per_pack_server_serves_only_its_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_AGENTS", "mendari/referral-router")
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    assert [p["id"] for p in client.get("/api/agents").json()] == ["mendari/referral-router"]
    assert client.get("/api/agents/mendari/patient-inbox-router").status_code == 404


def test_a_pack_that_is_not_installed_fails_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_AGENTS", "mendari/referal-router")  # a typo
    with pytest.raises(MendariError, match="referal-router"):
        create_app(db_path=tmp_path / "a.db")


def test_the_templates_parse_and_hold_no_secrets() -> None:
    compose = yaml.safe_load((DEPLOY / "onprem" / "docker-compose.yml").read_text())
    assert set(compose["services"]) == {"postgres", "engine", "jobs"}
    manifests = list(yaml.safe_load_all((DEPLOY / "kubernetes" / "mendari.yaml").read_text()))
    kinds = [m["kind"] for m in manifests]
    assert kinds == ["ConfigMap", "Deployment", "Service", "CronJob"]
    config = manifests[0]["data"]
    secret_names = {"MENDARI_PHI_KEY", "MENDARI_INPUT_HASH_KEY", "MENDARI_OIDC_CLIENT_SECRET"}
    assert not {"DATABASE_URL", *secret_names} & set(config)
    example = dict(
        line.split("=", 1)
        for line in (DEPLOY / "onprem" / ".env.example").read_text().splitlines()
        if line and not line.startswith("#")
    )
    for secret in ("POSTGRES_PASSWORD", *secret_names):
        assert example[secret] == "", secret


def test_compose_trusts_x_forwarded_for_from_the_gateway_a_proxy_on_the_host_comes_from() -> None:
    """Docker's port publishing connects to the engine from the network's gateway, not from
    127.0.0.1: the network's gateway is fixed, and FORWARDED_ALLOW_IPS defaults to it (in the
    compose file and in .env.example), so sign-in limits and the log see each person's address."""
    compose = yaml.safe_load((DEPLOY / "onprem" / "docker-compose.yml").read_text())
    (network,) = compose["networks"]["default"]["ipam"]["config"]
    gateway = ipaddress.ip_address(network["gateway"])
    assert gateway in ipaddress.ip_network(network["subnet"]) and str(gateway) == "172.30.0.1"
    engine = compose["services"]["engine"]
    assert engine["ports"] == ["127.0.0.1:8000:8000"]
    assert engine["environment"]["FORWARDED_ALLOW_IPS"] == "${FORWARDED_ALLOW_IPS:-172.30.0.1}"
    example = dict(
        line.split("=", 1)
        for line in (DEPLOY / "onprem" / ".env.example").read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert example["FORWARDED_ALLOW_IPS"] == str(gateway)


def test_only_the_sandbox_engine_answers_cds_hooks_from_the_network() -> None:
    """Open mode answers CDS Hooks on its own machine only (LP-23c), and `MENDARI_CDS_SANDBOX=1`
    lifts that for a sandbox on synthetic data. Only a synthetic sandbox's open engine sets it
    (the compose sandbox's and the Inferno harness's)
    (Docker's port publishing reaches it from a gateway, never from loopback): not its signed-in
    engine (where the flag would stop startup), and nothing a site deploys from `deploy/` (the
    on-prem compose file and its example settings, Kubernetes, Vercel) nor the hosted site."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    setting = "MENDARI_CDS_SANDBOX"
    flagged = {
        name
        for name, service in compose["services"].items()
        if setting in (service.get("environment") or {})
    }
    assert flagged == {"engine"}
    engine = compose["services"]["engine"]["environment"]
    assert engine[setting] == "1" and engine["MENDARI_PHI_MODE"] == "off"
    assert "MENDARI_OIDC_ISSUER" not in engine  # open mode: the flag stops startup with sign-in
    assert "MENDARI_OIDC_ISSUER" in compose["services"]["engine-signin"]["environment"]
    inferno = yaml.safe_load((ROOT / "sandbox" / "inferno" / "docker-compose.yml").read_text())
    harness = {n for n, s in inferno["services"].items() if setting in (s.get("environment") or {})}
    assert harness == {"engine"}  # the Inferno harness's open engine, on synthetic data (DV-c)
    assert inferno["services"]["engine"]["environment"][setting] == "1"
    assert inferno["services"]["engine"]["environment"]["MENDARI_PHI_MODE"] == "off"
    onprem = yaml.safe_load((DEPLOY / "onprem" / "docker-compose.yml").read_text())
    assert not any(setting in (s.get("environment") or {}) for s in onprem["services"].values())
    deployed = [*templates(), ROOT / "vercel.json", ROOT / "app.py", ROOT / "Dockerfile"]
    assert deployed and not [path for path in deployed if setting in path.read_text()]


def test_every_setting_the_templates_use_is_documented() -> None:
    """In the long reference or the README; the one-machine install's own settings (what its
    installer writes, what its compose file reads) in its one-page guide."""
    used = set()
    for path in templates():
        used |= set(re.findall(r"MENDARI_[A-Z_]+", path.read_text()))
    documented = (ROOT / "docs" / "deploying-with-phi.md").read_text() + (
        ROOT / "README.md"
    ).read_text()
    guide = GUIDE.read_text()
    assert sorted(s for s in used if s not in documented + guide) == []
    compose = (SITE / "docker-compose.yml").read_text()
    read = set(re.findall(r"\$\{([A-Z_]+)", compose))
    assert {"SITE_SUBNET", "SITE_PROXY_ADDRESS", "SITE_ENGINE_MEMORY"} <= read
    for name in sorted({*install.MANAGED, *read}):
        assert f"`{name}`" in guide, f"{name} is not in {GUIDE.relative_to(ROOT)}"


# -- the on-prem template: nothing passed over ----------------------------------------------------


def test_the_onprem_services_restart_and_its_jobs_never_drop_a_failure() -> None:
    """Each service comes back with the machine. A daily job that fails is named on stderr and
    turns the jobs' container unhealthy; with CRON_SECRET the checkpoint is the engine's own
    route, so an audit trail that does not verify reaches the alert webhook."""
    compose = yaml.safe_load((DEPLOY / "onprem" / "docker-compose.yml").read_text())
    services = compose["services"]
    assert {name: s.get("restart") for name, s in services.items()} == dict.fromkeys(
        services, "unless-stopped"
    )
    jobs = services["jobs"]
    loop = jobs["command"][-1]
    for job in ("mendari phi purge", "mendari audit checkpoint", "mendari monitor"):
        (line,) = [line for line in loop.splitlines() if job in line]
        assert "|| failed=" in line, line  # its exit code is kept
    assert "|| true" not in loop and "FAILED:$$failed" in loop
    assert "touch /tmp/jobs-failed" in loop and "rm -f /tmp/jobs-failed" in loop
    assert jobs["healthcheck"]["test"] == ["CMD-SHELL", "[ ! -e /tmp/jobs-failed ]"]
    asked = jobs["environment"]["CHECKPOINT_BY_THE_ENGINE"]
    assert "http://engine:8000/api/internal/checkpoint" in asked
    assert 'os.environ["CRON_SECRET"]' in asked and "sys.exit(0 if" in asked
    assert 'python -c "$$CHECKPOINT_BY_THE_ENGINE"' in loop
    compile(asked, "the checkpoint asked of the engine", "exec")  # it is Python, on one line
    example = (DEPLOY / "onprem" / ".env.example").read_text()
    assert "\nCRON_SECRET=\n" in example


# -- the one-machine template (deploy/site) -------------------------------------------------------


def site_compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((SITE / "docker-compose.yml").read_text())
    return loaded


LONG_RUNNING = ("postgres", "engine", "proxy", "jobs")


def test_the_site_template_parses_and_holds_no_secret() -> None:
    compose = site_compose()
    services = compose["services"]
    assert set(services) == {*LONG_RUNNING, "migrate"}
    # Every secret comes from .env, which the installer writes: none is in the template, and
    # without .env nothing starts, with a line that says what to run.
    text = (SITE / "docker-compose.yml").read_text()
    for name in ("MENDARI_IMAGE", "POSTGRES_PASSWORD", "CRON_SECRET", "SITE_HOST"):
        assert f"${{{name}:?run ./mendari-site install first, which writes .env}}" in text, name
    assert services["postgres"]["environment"]["POSTGRES_PASSWORD"].startswith("${")
    assert services["jobs"]["environment"]["PGPASSWORD"].startswith("${")
    assert services["jobs"]["environment"]["CRON_SECRET"].startswith("${")
    for name in ("engine", "migrate"):
        assert services[name]["env_file"] == ".env"
        assert services[name]["image"].startswith("${MENDARI_IMAGE:?")
        assert services[name]["environment"]["DATABASE_URL"] == (
            "postgresql://mendari:${POSTGRES_PASSWORD:?run ./mendari-site install first, "
            "which writes .env}@postgres:5432/mendari"
        )
    for path in templates():
        if SITE in path.parents:
            for secret in install.GENERATED:  # named, never given a value
                assert not re.search(rf"^{secret}=\S", path.read_text(), re.M), path
    # What an install makes here is never committed.
    ignored = (SITE / ".gitignore").read_text().split()
    assert {".env", ".env.*", "tls/", "backups/"} <= set(ignored)
    built = (ROOT / ".dockerignore").read_text().split()  # nor sent to an image build
    assert {"deploy/site/.env", "deploy/site/tls", "deploy/site/backups"} <= set(built)
    assert sorted(p.name for p in SITE.iterdir() if p.name not in INSTALLED) == [
        ".gitignore",
        "README.md",
        "docker-compose.yml",
        "jobs.sh",
        "mendari-site",
        "proxy.conf",
    ]


def test_every_site_service_restarts_is_bounded_and_says_when_it_is_unwell() -> None:
    services = site_compose()["services"]
    for name in LONG_RUNNING:
        service = services[name]
        assert service["restart"] == "unless-stopped", name
        assert service["deploy"]["resources"]["limits"]["memory"].startswith("${SITE_"), name
        assert service["logging"] == {
            "driver": "json-file",
            "options": {"max-size": "20m", "max-file": "5"},
        }, name
        assert service["healthcheck"]["test"], name
        assert "no-new-privileges:true" in service["security_opt"], name
    # The migration runs once at each start, never again by itself, and the engine waits for
    # it: a migration that fails starts no engine on that database.
    migrate = services["migrate"]
    assert migrate["restart"] == "no"
    assert migrate["command"] == ["python", "-m", "mendari.install", "migrate"]
    assert services["engine"]["depends_on"] == {
        "postgres": {"condition": "service_healthy"},
        "migrate": {"condition": "service_completed_successfully"},
    }
    assert services["proxy"]["depends_on"] == {"engine": {"condition": "service_healthy"}}
    # The engine drops every capability; so do the jobs.
    assert services["engine"]["cap_drop"] == ["ALL"] and services["jobs"]["cap_drop"] == ["ALL"]
    # The engine's liveness does no database work, as the image's own check.
    assert "/api/health'" in " ".join(services["engine"]["healthcheck"]["test"])
    # A day's jobs that failed turn their container unhealthy until a run passes.
    assert ".jobs-status" in services["jobs"]["healthcheck"]["test"][1]


def test_only_the_front_is_published_and_the_database_keeps_its_data_in_a_volume() -> None:
    compose = site_compose()
    services = compose["services"]
    published = {name: s["ports"] for name, s in services.items() if "ports" in s}
    assert published == {"proxy": ["${SITE_HTTPS_PORT:-443}:443", "${SITE_HTTP_PORT:-80}:80"]}
    assert services["postgres"]["volumes"] == ["postgres-data:/var/lib/postgresql/data"]
    assert set(compose["volumes"]) == {"postgres-data", "state"}
    # What the engine pulls from a registry, and the registry keys it pins, outlive an upgrade:
    # the installer points the engine at the state volume, which every engine container mounts.
    for name in ("engine", "migrate"):
        assert services[name]["volumes"] == ["state:/data"], name
    assert install.STATE_CACHE.startswith("/data/") and install.STATE_CONFIG.startswith("/data/")
    # The backups are on the host, in the install's folder, owned by whoever installed.
    assert "./backups:/backups:z" in services["jobs"]["volumes"]
    assert services["jobs"]["user"] == "${SITE_UID:-0}:${SITE_GID:-0}"
    # The images that are not the engine's are pinned by digest, and the jobs dump with the
    # database's own version.
    assert services["jobs"]["image"] == services["postgres"]["image"]
    assert "@sha256:" in services["postgres"]["image"] and "@sha256:" in services["proxy"]["image"]


def test_the_site_engine_trusts_forwarded_addresses_from_the_front_alone() -> None:
    compose = site_compose()
    (network,) = compose["networks"]["default"]["ipam"]["config"]
    subnet = re.fullmatch(r"\$\{SITE_SUBNET:-(.+)\}", network["subnet"])
    assert subnet is not None
    proxy = compose["services"]["proxy"]["networks"]["default"]["ipv4_address"]
    address = re.fullmatch(r"\$\{SITE_PROXY_ADDRESS:-(.+)\}", proxy)
    assert address is not None
    assert ipaddress.ip_address(address[1]) in ipaddress.ip_network(subnet[1])
    for name in ("engine", "migrate"):
        trusted = compose["services"][name]["environment"]["FORWARDED_ALLOW_IPS"]
        assert trusted == proxy, name  # that one address, and no other container's
    conf = (SITE / "proxy.conf").read_text()
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in conf  # overwritten
    assert "$proxy_add_x_forwarded_for" not in conf  # never appended to what a client sent
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in conf
    assert "proxy_set_header Host $http_host;" in conf


def servers(conf: str) -> list[str]:
    """Each `server { ... }` block of an nginx configuration, as text."""
    found, depth, start = [], 0, None
    for index, character in enumerate(conf):
        if character == "{":
            if depth == 0 and conf[:index].rstrip().endswith("server"):
                start = index
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0 and start is not None:
                found.append(conf[start : index + 1])
                start = None
    return found


def test_the_front_serves_https_only_and_limits_sign_in_per_address() -> None:
    conf = "\n".join(
        line for line in (SITE / "proxy.conf").read_text().splitlines() if not line.startswith("#")
    )
    blocks = servers(conf)
    assert len(blocks) == 4
    health, plain, other, main = blocks
    assert "listen 127.0.0.1:8081;" in health  # asked from inside its own container only
    # Plain http serves nothing: it only sends the browser to https, at the public address.
    assert "listen 80 default_server;" in plain and "proxy_pass" not in plain
    assert "return 308 https://${SITE_HOST}:${SITE_HTTPS_PORT}$request_uri;" in plain
    # A name that is not the public address's is not served.
    assert "listen 443 ssl default_server;" in other and "return 421;" in other
    assert "proxy_pass" not in other
    assert "server_name ${SITE_HOST};" in main and "listen 443 ssl;" in main
    for block in (other, main):
        assert "ssl_protocols TLSv1.2 TLSv1.3;" in block
        assert "ssl_certificate     /etc/nginx/tls/fullchain.pem;" in block
        assert "ssl_certificate_key /etc/nginx/tls/privkey.pem;" in block
    assert conf.count("listen 80") == 1 and "listen 8000" not in conf
    # Sign-in and the EHR launch are limited per address, below Mendari's own limit; the
    # scheduled jobs' routes are not reachable from outside.
    assert "limit_req_zone $binary_remote_addr zone=mendari_signin:10m rate=2r/s;" in conf
    for location in ("/auth/", "/smart/"):
        inside = main.split(f"location {location} {{", 1)[1].split("}", 1)[0]
        assert "limit_req zone=mendari_signin burst=60 nodelay;" in inside, location
    internal = main.split("location /api/internal/ {", 1)[1].split("}", 1)[0]
    assert internal.split() == ["return", "404;"]
    assert "limit_req_status 429;" in conf and "server_tokens off;" in conf
    # The names filled in when the container starts are these two and no other: nginx's own
    # variables stay.
    proxy = site_compose()["services"]["proxy"]
    assert proxy["environment"]["NGINX_ENVSUBST_FILTER"] == "^SITE_"
    assert set(re.findall(r"\$\{(\w+)\}", conf)) == {"SITE_HOST", "SITE_HTTPS_PORT"}
    assert "./proxy.conf:/etc/nginx/templates/default.conf.template:ro,z" in proxy["volumes"]
    assert "./tls:/etc/nginx/tls:ro,z" in proxy["volumes"]


def test_no_log_of_the_site_holds_a_query_string_a_body_or_a_statement() -> None:
    """A sign-in callback's code and a search's values are in the query; a case's text is in a
    body, and in a statement's parameters."""
    conf = "\n".join(
        line for line in (SITE / "proxy.conf").read_text().splitlines() if not line.startswith("#")
    )
    (written,) = re.findall(r"log_format mendari ([^;]+);", conf)
    assert set(re.findall(r"\$\w+", written)) == {
        "$time_iso8601",
        "$remote_addr",
        "$request_method",
        "$uri",  # the path alone: never $request, $request_uri or $args
        "$status",
        "$request_time",
        "$upstream_status",
    }
    # Every server names the format itself, or writes nothing: one without would inherit the
    # image's own, which writes the whole request line.
    for block in servers(conf):
        assert re.findall(r"access_log ([^;]+);", block) in (["off"], ["/dev/stdout mendari"])
    assert re.findall(r"access_log ([^;]+);", conf).count("/dev/stdout mendari") == 3
    # An error's line names the whole request line: only what stops the front is written.
    assert re.findall(r"error_log ([^;]+);", conf) == ["/dev/stderr crit"]
    # The database's log holds no statement and no parameter.
    command = site_compose()["services"]["postgres"]["command"]
    for setting in (
        "log_statement=none",
        "log_min_error_statement=panic",
        "log_parameter_max_length_on_error=0",
    ):
        assert setting in command, setting
    # The engine's own log is tested where it is written (tests/test_security_log.py); the
    # template turns on nothing that adds to it.
    engine = site_compose()["services"]["engine"]
    assert "--access-log" not in " ".join(engine["command"]) and "MENDARI_DEBUG" not in str(engine)


# -- the daily jobs, run for real with stand-ins for the engine and the database's tools ----------

# What the jobs show the engine, made up for these tests and marked, to search their output for.
CRON = "zq-jobs-secret-0123456789abcdef"
# `wget`, as the jobs call it: writes down what it was asked (never the secret itself, only
# whether it was the right one) and answers from the files the test wrote.
WGET = """#!/bin/sh
url= ; auth=none ; post=GET
while [ $# -gt 0 ]; do
    case $1 in
    --header) [ "$2" = "Authorization: Bearer $EXPECTED_SECRET" ] && auth=ok || auth=wrong; shift ;;
    --post-data) post=POST; shift ;;
    -T|-O) shift ;;
    http*) url=$1 ;;
    esac
    shift
done
path=${url#http://engine.test}
printf '%s %s %s\\n' "$post" "$path" "$auth" >>"$CALLS"
name=$(printf '%s' "${path%%\\?*}" | tr '/' '_')
[ -f "$ANSWERS/$name.fail" ] && exit 8
[ -f "$ANSWERS/$name" ] && cat "$ANSWERS/$name"
exit 0
"""
PG_DUMP = """#!/bin/sh
[ -f "$ANSWERS/pg_dump.fail" ] && exit 1
while [ $# -gt 0 ]; do
    case $1 in --file) printf 'a dump\\n' >"$2"; shift ;; esac
    shift
done
"""
PG_RESTORE = """#!/bin/sh
[ -f "$ANSWERS/pg_restore.fail" ] && exit 1
exit 0
"""
# `psql`, asked whether the audit trail holds a fork: answers with what the test wrote.
PSQL = """#!/bin/sh
printf 'psql %s\\n' "$*" >>"$CALLS"
[ -f "$ANSWERS/psql.fail" ] && exit 1
[ -f "$ANSWERS/psql.rows" ] && cat "$ANSWERS/psql.rows"
exit 0
"""
# `sleep` that sleeps nothing and stops the loop at its second call.
SLEEP = """#!/bin/sh
printf '%s\\n' "$1" >>"$SLEPT"
[ "$(wc -l <"$SLEPT")" -ge 2 ] && kill -TERM "$PPID"
exit 0
"""
HELD = {
    "_api_internal_retention": '{"inputs":0,"notes":0}',
    "_api_internal_checkpoint": '{"ok":true,"recorded":true,"signed":true}',
    "_api_internal_monitor": '{"organizations":1,"alerting":0,"delivered":0,"not_delivered":0}',
    "_api_internal_backup": '{"recorded":true}',
}


class Jobs:
    """`deploy/site/jobs.sh` in a folder of its own, with stand-ins on its PATH."""

    def __init__(self, folder: Path):
        self.backups = folder / "backups"
        self.answers = folder / "answers"
        self.tools = folder / "tools"
        for made in (self.backups, self.answers, self.tools):
            made.mkdir()
        tools = {"wget": WGET, "pg_dump": PG_DUMP, "pg_restore": PG_RESTORE, "sleep": SLEEP}
        tools["psql"] = PSQL
        for name, script in tools.items():
            tool = self.tools / name
            tool.write_text(script)
            tool.chmod(0o755)
        for name, body in HELD.items():
            (self.answers / name).write_text(body)
        self.calls = folder / "calls"
        self.slept = folder / "slept"

    def run(self, *command: str) -> subprocess.CompletedProcess[str]:
        self.calls.write_text("")
        environment = {
            "PATH": f"{self.tools}{os.pathsep}{os.environ['PATH']}",
            "CRON_SECRET": CRON,
            "EXPECTED_SECRET": CRON,
            "ENGINE_URL": "http://engine.test",
            "BACKUPS": str(self.backups),
            "BACKUP_KEEP_DAYS": "14",
            "PGDATABASE": "mendari",
            "CALLS": str(self.calls),
            "ANSWERS": str(self.answers),
            "SLEPT": str(self.slept),
        }
        return subprocess.run(
            ["sh", str(SITE / "jobs.sh"), *command],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def asked(self) -> list[str]:
        return self.calls.read_text().splitlines()

    def status(self) -> str:
        return (self.backups / ".jobs-status").read_text().strip()

    def dumps(self) -> list[str]:
        return sorted(p.name for p in self.backups.iterdir() if not p.name.startswith(".jobs-"))


EVERY_JOB = [
    "GET /api/health none",
    "GET /api/internal/retention ok",
    "GET /api/internal/checkpoint ok",
    "GET /api/internal/monitor ok",
    "POST /api/internal/backup?outcome=ok ok",
]


@posix
def test_the_daily_jobs_ask_the_engine_with_their_secret_and_back_the_database_up(
    tmp_path: Path,
) -> None:
    jobs = Jobs(tmp_path)
    ran = jobs.run("once")
    assert ran.returncode == 0, ran.stderr
    assert jobs.asked() == EVERY_JOB  # in order, each with the jobs' secret
    assert jobs.status() == "ok"
    (dump,) = jobs.dumps()
    assert re.fullmatch(r"mendari-\d{8}T\d{6}Z\.dump", dump)
    assert stat.S_IMODE((jobs.backups / dump).stat().st_mode) == 0o600  # its owner's alone
    assert "the daily jobs passed" in ran.stdout and ran.stderr == ""
    assert CRON not in ran.stdout + ran.stderr
    # A backup now, by hand: the same, reported to the engine.
    again = jobs.run("backup")
    assert again.returncode == 0 and jobs.asked() == ["POST /api/internal/backup?outcome=ok ok"]
    # Backups past their days are dropped; the newer ones stay.
    old = jobs.backups / "mendari-20200101T000000Z.dump"
    old.write_text("an old dump")
    os.utime(old, (0, 0))
    jobs.run("backup")
    assert old.name not in jobs.dumps() and dump in jobs.dumps()


@posix
@pytest.mark.parametrize(
    ("broken", "said", "reported"),
    [
        # The audit trail does not verify: the engine answered, and said so.
        (
            {"_api_internal_checkpoint": '{"ok":false,"recorded":false,"signed":false}'},
            "checkpoint: THE AUDIT TRAIL DOES NOT VERIFY",
            "ok",
        ),
        (
            {"_api_internal_checkpoint.fail": ""},
            "checkpoint: the audit trail was not checked",
            "ok",
        ),
        ({"_api_internal_retention.fail": ""}, "retention: kept inputs past their retention", "ok"),
        (
            {"_api_internal_monitor": '{"organizations":1,"alerting":1,"not_delivered":1}'},
            "monitor: an alert could not be delivered",
            "ok",
        ),
        ({"_api_internal_monitor.fail": ""}, "monitor: the agents were not checked", "ok"),
        ({"pg_dump.fail": ""}, "backup: the database could not be backed up", "failed"),
        # Written, and not readable: it never takes a backup's name.
        ({"pg_restore.fail": ""}, "backup: the database could not be backed up", "failed"),
    ],
)
def test_a_daily_job_that_fails_is_said_and_never_stops_the_others(
    tmp_path: Path, broken: dict[str, str], said: str, reported: str
) -> None:
    jobs = Jobs(tmp_path)
    for name, body in broken.items():
        (jobs.answers / name).write_text(body)
    ran = jobs.run("once")
    assert ran.returncode == 1  # the run failed, and says so to whoever started it
    assert f"FAILED: {said}" in ran.stderr and "the daily jobs FAILED" in ran.stderr
    assert jobs.status() == "failed"  # the container is unhealthy until a run passes
    # Every other job still ran, and the backup was reported as it ended.
    assert jobs.asked() == [*EVERY_JOB[:4], f"POST /api/internal/backup?outcome={reported} ok"]
    assert len(jobs.dumps()) == (1 if reported == "ok" else 0)  # never a partial backup
    assert CRON not in ran.stdout + ran.stderr
    # The next run that passes clears it.
    for name in broken:
        (jobs.answers / name).unlink()
    for name, body in HELD.items():
        (jobs.answers / name).write_text(body)
    assert jobs.run("once").returncode == 0 and jobs.status() == "ok"


@posix
def test_the_daily_loop_waits_out_the_day_after_a_restart(tmp_path: Path) -> None:
    """A restart of the machine does not run everything again, nor send an alert twice: the
    loop first waits for what is left of the day since the last run."""
    jobs = Jobs(tmp_path)
    assert jobs.run("once").returncode == 0
    ran = jobs.run("daily")  # the stand-in `sleep` stops the loop at its second call
    slept = [int(line) for line in jobs.slept.read_text().split()]
    assert len(slept) == 2 and 86300 < slept[0] <= 86400 and slept[1] == 86400
    assert f"the next run is in {slept[0]} s" in ran.stdout
    assert jobs.asked() == EVERY_JOB  # then the jobs, once
    # With no run on record the jobs run at once, then a day apart.
    (tmp_path / "fresh").mkdir()
    fresh = Jobs(tmp_path / "fresh")
    fresh.run("daily")
    assert [int(line) for line in fresh.slept.read_text().split()] == [86400, 86400]
    assert fresh.asked() == EVERY_JOB * 2


@posix
def test_the_fork_check_before_an_upgrade_is_read_only_and_fails_on_a_fork(
    tmp_path: Path,
) -> None:
    """Two events chained after the same one: a version whose start gives the chain its unique
    index cannot take such a database (the incident runbook), so an upgrade asks first."""
    jobs = Jobs(tmp_path)
    clean = jobs.run("forks")
    assert clean.returncode == 0 and "no fork in the audit trail" in clean.stdout
    (asked,) = jobs.asked()
    assert "BEGIN READ ONLY; SELECT 1 FROM audit_events GROUP BY prev_hash" in asked
    assert asked.rstrip().endswith("ROLLBACK;") and "ON_ERROR_STOP=1" in asked
    (jobs.answers / "psql.rows").write_text("1\n")
    forked = jobs.run("forks")
    assert forked.returncode == 1 and "THE AUDIT TRAIL HOLDS A FORK" in forked.stderr
    (jobs.answers / "psql.fail").write_text("")
    unread = jobs.run("forks")  # a database that cannot be asked is not a database that passed
    assert unread.returncode == 1 and "could not be read for forks" in unread.stderr
    # An upgrade runs both checks, in its first step, before anything is migrated.
    upgrade = (SITE / "mendari-site").read_text().split("cmd_upgrade() {", 1)[1]
    verify = upgrade.index('in_engine "$VERIFY" ||')
    forks = upgrade.index("in_jobs forks ||")
    migrate = upgrade.index("4 of 6. The database, brought up to date by the new version")
    assert 0 < verify < forks < migrate


@posix
def test_the_jobs_take_only_the_names_a_restore_makes(tmp_path: Path) -> None:
    jobs = Jobs(tmp_path)
    for command in (
        ["restore-into", "mendari; DROP DATABASE mendari", "mendari-20261124T000000Z.dump"],
        ["restore-into", "mendari_restore_1", "../../etc/passwd"],
        ["restore-into", "mendari_restore_1", "other.dump"],
        ["swap", "Mendari", "mendari_before_1"],
        ["swap", "mendari_restore_1", 'x" OWNER TO postgres; --'],
        ["drop", "a b"],
        ["drop", ""],
        ["nonsense"],
    ):
        ran = jobs.run(*command)
        assert ran.returncode == 2, command
        assert jobs.asked() == [] and jobs.dumps() == []


# -- the script: what it refuses before it touches anything ---------------------------------------

# `docker`, as the script calls it: answers that it is there, writes down what it was asked,
# and does nothing; asked to start the services, it fails as $UP_FAILS says, when set.
DOCKER = """#!/bin/sh
printf '%s\\n' "$*" >>"$CALLS"
case "$*" in
*"compose version"*|info) exit 0 ;;
*" ps "*) exit 0 ;;
*" up -d"*) [ -z "${UP_FAILS:-}" ] || { printf '%s\\n' "$UP_FAILS" >&2; exit 1; } ;;
esac
exit 0
"""


class Script:
    """A copy of the template in a folder of its own, with a stand-in `docker` on its PATH."""

    def __init__(self, folder: Path, *, docker: bool = True):
        self.site = folder / "site"
        shutil.copytree(SITE, self.site, ignore=shutil.ignore_patterns(*INSTALLED))
        self.tools = folder / "tools"
        self.tools.mkdir()
        self.calls = folder / "calls"
        self.calls.write_text("")
        self.system = f"/usr/bin{os.pathsep}/bin"
        if docker:
            tool = self.tools / "docker"
            tool.write_text(DOCKER)
            tool.chmod(0o755)
        elif shutil.which("docker", path=self.system):
            # A machine with Docker among the system's own tools (a CI runner): every one of
            # them but that, by link, so the script finds no Docker here either.
            bare = folder / "system"
            bare.mkdir()
            for directory in ("/bin", "/usr/bin"):
                for found in Path(directory).iterdir():
                    link = bare / found.name
                    if not found.name.startswith("docker") and not link.is_symlink():
                        link.symlink_to(found)
            self.system = str(bare)

    def run(self, *command: str, stdin: str = "", **more: str) -> subprocess.CompletedProcess[str]:
        # Only the stand-in and the system's own tools: never a real Docker.
        environment = {
            "PATH": f"{self.tools}{os.pathsep}{self.system}",
            "CALLS": str(self.calls),
            "HOME": str(self.site.parent),
            "TMPDIR": str(self.site.parent),
            **more,
        }
        return subprocess.run(
            ["sh", str(self.site / "mendari-site"), *command],
            env=environment,
            capture_output=True,
            text=True,
            input=stdin,
            timeout=60,
            check=False,
        )

    def docker_calls(self) -> list[str]:
        return self.calls.read_text().splitlines()

    def installed(self, **values: str) -> None:
        settings = {"MENDARI_IMAGE": "mendari-engine:site-0123456789ab", **values}
        (self.site / ".env").write_text("".join(f"{k}={v}\n" for k, v in settings.items()))


@posix
def test_the_scripts_are_posix_shell_that_can_be_run() -> None:
    for name in ("mendari-site", "jobs.sh"):
        script = SITE / name
        lines = script.read_text().splitlines()
        assert lines[0] == "#!/bin/sh" and lines[1] == "# SPDX-License-Identifier: Apache-2.0"
        assert os.access(script, os.X_OK), f"{name} is not executable"
        checked = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
        assert checked.returncode == 0, checked.stderr
        text = script.read_text()
        assert "[[" not in text and "function " not in text  # nothing a POSIX shell lacks
        assert "set -eu" in text or "set -u" in text


@posix
def test_the_script_lists_its_commands_and_refuses_one_it_does_not_have(tmp_path: Path) -> None:
    script = Script(tmp_path)
    helped = script.run("help")
    assert helped.returncode == 0 and script.run().stdout == helped.stdout
    commands = re.findall(r"^  \./mendari-site (\w+)", helped.stdout, re.M)
    assert commands == [
        "install",
        "status",
        "start",
        "stop",
        "upgrade",
        "rollback",
        "backup",
        "restore",
        "verify",
        "hold",
        "holds",
        "lift",
        "logs",
        "forget",
    ]
    text = (SITE / "mendari-site").read_text()
    for command in commands:  # each listed command is one the script runs
        assert re.search(rf"^{command}\) cmd_{command}\b", text, re.M), command
    assert helped.stdout.rstrip().endswith("--no-start.")  # the whole of its head, and no more
    unknown = script.run("destroy")
    assert unknown.returncode == 1 and "no command 'destroy'" in unknown.stderr
    assert script.docker_calls() == []


@posix
def test_the_script_says_what_is_missing_before_it_touches_anything(tmp_path: Path) -> None:
    bare = Script(tmp_path / "bare", docker=False)
    ran = bare.run("install")
    assert ran.returncode == 1 and "Docker is not installed" in ran.stderr
    assert sorted(p.name for p in bare.site.iterdir()) == sorted(
        p.name for p in SITE.iterdir() if p.name not in INSTALLED
    )  # nothing was made
    script = Script(tmp_path / "docker")
    for command in ("status", "start", "stop", "backup", "verify", "holds", "rollback", "upgrade"):
        ran = script.run(command)
        assert ran.returncode == 1, command
        assert "there is no .env here yet: run ./mendari-site install" in ran.stderr, command
    assert script.docker_calls() == []
    # A settings file its reader cannot read stops everything, install included.
    script.installed()
    if os.geteuid() != 0:
        (script.site / ".env").chmod(0)
        for command in ("install", "start", "upgrade", "backup"):
            ran = script.run(command)
            assert ran.returncode == 1 and "cannot be read by you" in ran.stderr, command
        assert "never made again over it" in script.run("install").stderr
        (script.site / ".env").chmod(0o600)
        # Docker was asked whether it is there, and for nothing else.
        assert set(script.docker_calls()) <= {"compose version", "info"}


@posix
@pytest.mark.parametrize(
    ("command", "said"),
    [
        (["install", "--image", "ghcr.io/mendari-hq/mendari-engine:v0.1.0"], "by its digest"),
        (["upgrade", "--image", "mendari-engine:latest"], "by its digest"),
        (["install", "--answers"], "--answers needs a file"),
        (["install", "--answers", "/nowhere/answers"], "cannot be read"),
        (["install", "--source", "/nowhere/mendari"], "there is no folder /nowhere/mendari"),
        (["install", "--force"], "no option --force"),
        (["upgrade", "--force"], "no option --force"),
        (["hold"], "say why, in a few words"),
        (["lift"], "name the hold"),
        (["restore"], "name the backup to restore"),
        (["restore", "backups/other.sql"], "is not a backup this install made"),
        (["restore", "backups/mendari-20261124T000000Z.dump"], "there is no mendari-2026"),
        (["forget"], "name a database a restore kept"),
        (["forget", "mendari"], "name a database a restore kept"),
        (["forget", "postgres"], "name a database a restore kept"),
    ],
)
def test_the_script_refuses_what_it_cannot_do_safely(
    tmp_path: Path, command: list[str], said: str
) -> None:
    script = Script(tmp_path)
    script.installed()
    ran = script.run(*command)
    assert ran.returncode == 1 and said in ran.stderr, ran.stderr
    # Nothing was run in a container, started, stopped or pulled for it.
    touched = ("run ", "up ", "stop", "pull", "build", "exec ")
    assert not [
        call
        for call in script.docker_calls()
        if call.startswith(touched) or any(f" {word}" in call for word in touched)
    ], script.docker_calls()
    assert (script.site / ".env").read_text() == "MENDARI_IMAGE=mendari-engine:site-0123456789ab\n"


@posix
def test_a_restore_asks_first_and_does_nothing_without_the_word(tmp_path: Path) -> None:
    script = Script(tmp_path)
    script.installed()
    (script.site / "backups").mkdir()
    (script.site / "backups" / "mendari-20261124T000000Z.dump").write_text("a dump")
    ran = script.run("restore", "backups/mendari-20261124T000000Z.dump", stdin="yes\n")
    assert ran.returncode == 1 and "not restored. Nothing was changed." in ran.stderr
    assert "Type the word restore to go on" in ran.stdout
    assert "is kept beside it" in ran.stdout  # what it will do, said before it is done
    assert not [call for call in script.docker_calls() if " stop" in call or " run " in call]


@posix
@pytest.mark.parametrize(
    ("docker_said", "plainly"),
    [
        (
            "Error response from daemon: Ports are not available: exposing port TCP "
            "0.0.0.0:443 -> 127.0.0.1:0: listen tcp 0.0.0.0:443: bind: address already in use",
            "A port this deployment listens on (8443 for https, 8080 for http) is in use by "
            "something else on this machine.",
        ),
        (
            "Error response from daemon: driver failed programming external connectivity: "
            "Bind for 0.0.0.0:80 failed: port is already allocated",
            "A port this deployment listens on",
        ),
        (
            "failed to create network mendari-onsite_default: Error response from daemon: "
            "invalid pool request: Pool overlaps with other one on this address space",
            "The private network range this deployment uses is taken on this machine. Add "
            "SITE_SUBNET and SITE_PROXY_ADDRESS",
        ),
        ('service "migrate" didn\'t complete successfully: exit 1', ""),
    ],
)
def test_when_docker_cannot_start_the_services_the_reason_is_said_last_and_plainly(
    tmp_path: Path, docker_said: str, plainly: str
) -> None:
    """A port taken by another web server is the usual first failure on a hospital's machine:
    what Docker said is the last thing on the screen, with what it means."""
    script = Script(tmp_path)
    script.installed(SITE_HTTPS_PORT="8443", SITE_HTTP_PORT="8080")
    ran = script.run("start", UP_FAILS=docker_said)
    assert ran.returncode == 1
    said = ran.stderr.strip().splitlines()
    assert said[-1].startswith("mendari-site: Mendari did not come up.")
    after = ran.stderr.split("What Docker said last:", 1)[1]
    assert docker_said in ran.stdout and docker_said in after  # as it came, and again at the end
    if plainly:
        assert plainly in after
    else:
        assert "A port" not in after and "private network" not in after
    # The steps before it ran in order, and nothing was left behind.
    calls = script.docker_calls()
    order = [
        next(i for i, c in enumerate(calls) if word in c) for word in ("check", "migrate", "up -d")
    ]
    assert order == sorted(order)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["calls", "site", "tools"]
