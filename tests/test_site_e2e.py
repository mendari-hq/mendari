# SPDX-License-Identifier: Apache-2.0
"""The one-machine install, run for real with Docker, end to end (`deploy/site`):

    MENDARI_SITE_E2E=1 uv run pytest tests/test_site_e2e.py -n0 -p no:cacheprovider

Skipped unless asked for: it builds the engine's image from this copy of the repository and
runs the whole stack, which takes a few minutes and needs Docker with Compose. It uses a copy
of the template in a temporary folder, a Compose project name, a subnet and ports of its own
(never a real install's), a stand-in identity provider on the stack's own network, and
synthetic people; it removes its containers, network and volumes when it ends.

What it does, in order:

1. `./mendari-site install --answers ... --source <this repository>`: every secret generated,
   the settings checked as the server reads them, the database brought up to date, everything
   started and healthy behind the TLS front.
2. The front: https answers, plain http only redirects, another host name is not served, the
   scheduled jobs' routes are not reachable from outside, and no query string is logged.
3. The daily jobs ran: retention, the checkpoint, the drift check and a backup.
4. A real sign-in through the identity provider: someone else belongs nowhere; the
   administrator the install named becomes the operator, and the System checks answer.
5. Installing again changes nothing: the settings file is the same, byte for byte.
6. A backup, more decisions, then a restore: the restored audit trail verifies with the head
   it had at the backup, all automation is held, and the hold is lifted from the command line.
7. Fail closed: with a secret missing from the settings, `start` refuses and what was serving
   still serves.
8. An upgrade to a new build of the image: the audit trail verified read-only and checked for
   forks by the new version, a backup, the database brought up to date, the restart; then a
   rollback to the image before.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import socket
import stat
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from mendari import install
from mendari.server.origin import OWN_REQUEST
from tests.conftest import ROOT

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("MENDARI_SITE_E2E") != "1",
        reason="runs the one-machine install with Docker: set MENDARI_SITE_E2E=1 to run it",
    ),
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is not installed"),
]

SITE = ROOT / "deploy" / "site"
# The stand-in identity provider: a real OpenID Connect server with a scriptable sign-in form,
# the one CI's pilot check uses, by the same digest.
IDP_IMAGE = (
    "ghcr.io/navikt/mock-oauth2-server:6.0.4"
    "@sha256:47374fe063995d9b6d39ebb6ea21eb5f765025bf0aa08b61b08b439eb91adaf0"
)
IDP_NAME = "http://idp:8080"  # as the engine reaches it, and as its tokens name their issuer
ADA = ("ada", "ada@stmarys.example", "Ada L.")
BO = ("bo", "bo@stmarys.example", "Bo K.")
CLIENT_SECRET = "zq-e2e-client-secret"
DEMO = "mendari/portal-message-agent"


def free_port() -> int:
    """A port nothing listens on, on any address of this machine, IPv4 or IPv6: Docker publishes
    on both. Chosen below the range the system hands out by itself, where another process (a
    test server of someone else's run) could take it a moment later."""
    for _ in range(200):
        port = random.randint(20000, 29999)
        try:
            for family, every_address in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
                with socket.socket(family) as sock:
                    sock.bind((every_address, port))
        except OSError:
            continue
        return port
    raise AssertionError("no free port between 20000 and 29999")


class Install:
    """A copy of the template in a folder of its own, driven through its script."""

    def __init__(self, folder: Path):
        self.folder = folder / "site"
        shutil.copytree(SITE, self.folder, ignore=shutil.ignore_patterns(".env", "tls", "backups"))
        self.project = f"mendari-e2e-{os.getpid()}"
        self.https, self.http, self.idp_port = free_port(), free_port(), free_port()
        self.base = f"https://localhost:{self.https}"
        self.environment = {
            **os.environ,
            "COMPOSE_PROJECT_NAME": self.project,
            # A subnet of its own: a real install on this machine keeps the template's.
            "SITE_SUBNET": "172.31.88.0/24",
            "SITE_PROXY_ADDRESS": "172.31.88.10",
        }
        self.clients: list[httpx.Client] = []
        self.answers = folder / "answers"
        self.answers.write_text(
            f"PUBLIC_URL={self.base}\n"
            f"HTTP_PORT={self.http}\n"
            f"OIDC_ISSUER={IDP_NAME}/e2e\n"
            "OIDC_CLIENT_ID=mendari\n"
            f"OIDC_CLIENT_SECRET={CLIENT_SECRET}\n"
            f"ADMIN_EMAIL={ADA[1]}\n"
            'ORGANIZATION="St Mary\'s Hospital"\n'
            "PATIENT_DATA=yes\n"
            "SELF_SIGNED=yes\n"
            "TIMEZONE=Europe/Paris\n"
        )

    def run(self, *command: str, timeout: int = 900) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(self.folder / "mendari-site"), *command],
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def docker(self, *command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", *command], env=self.environment, capture_output=True, text=True, check=False
        )

    def settings(self) -> str:
        return (self.folder / ".env").read_text()

    def remove(self) -> None:
        for client in self.clients:
            client.close()
        self.docker("rm", "-f", f"{self.project}-idp")
        self.docker(
            "compose",
            "--project-directory",
            str(self.folder),
            "-f",
            str(self.folder / "docker-compose.yml"),
            "down",
            "--volumes",
            "--remove-orphans",
        )

    def start_identity_provider(self) -> None:
        started = self.docker(
            "run",
            "-d",
            "--name",
            f"{self.project}-idp",
            "--network",
            f"{self.project}_default",
            "--network-alias",
            "idp",
            "-p",
            f"127.0.0.1:{self.idp_port}:8080",
            "-e",
            'JSON_CONFIG={"interactiveLogin": true}',
            IDP_IMAGE,
        )
        assert started.returncode == 0, started.stderr
        here = f"http://127.0.0.1:{self.idp_port}"
        for _ in range(60):
            try:
                found = httpx.get(
                    f"{here}/e2e/.well-known/openid-configuration", headers={"Host": "idp:8080"}
                )
                if found.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise AssertionError("the stand-in identity provider did not start")

    def sign_in(self, person: tuple[str, str, str]) -> httpx.Client:
        """The browser's part of the OpenID Connect flow, through the TLS front: the engine's
        `/auth/login`, the provider's form, the engine's callback."""
        subject, email, name = person
        own = {OWN_REQUEST: "1"}  # as each request of the console says it is the console's
        http = httpx.Client(base_url=self.base, verify=False, headers=own, timeout=60)
        self.clients.append(http)  # closed with the install
        started = http.get("/auth/login")
        assert started.status_code in (302, 307), started.text[:200]
        authorize = started.headers["location"]
        assert authorize.startswith(f"{IDP_NAME}/e2e/authorize?"), authorize[:120]
        claims = {"sub": subject, "name": name, "email": email, "email_verified": True}
        signed = httpx.post(
            authorize.replace(IDP_NAME, f"http://127.0.0.1:{self.idp_port}", 1),
            headers={"Host": "idp:8080"},
            data={"username": subject, "claims": json.dumps(claims)},
            timeout=60,
        )
        assert signed.status_code in (302, 303), signed.text[:300]
        callback = signed.headers["location"]
        assert callback.startswith(f"{self.base}/auth/callback?"), callback[:120]
        done = http.get(callback)
        assert done.status_code in (302, 307), done.text[:300]
        return http


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Install]:
    made = Install(tmp_path)
    try:
        yield made
    finally:
        made.remove()


def head(output: str) -> dict[str, Any]:
    """The audit trail's verification as `mendari audit verify` prints it."""
    found = re.search(r"\{\s*\"ok\".*?\}", output, re.S)
    assert found is not None, output
    verified: dict[str, Any] = json.loads(found.group(0))
    return verified


def test_the_one_machine_install_end_to_end(site: Install) -> None:
    # 1. One command.
    installed = site.run("install", "--answers", str(site.answers), "--source", str(ROOT))
    assert installed.returncode == 0, installed.stdout[-3000:] + installed.stderr[-3000:]
    settings = site.settings()
    values = install.parse(settings)
    assert stat.S_IMODE((site.folder / ".env").stat().st_mode) == 0o600
    assert values["MENDARI_IMAGE"].startswith("mendari-engine:site-")
    assert (values["MENDARI_ACCOUNTS"], values["MENDARI_OPERATOR_ORG"]) == (
        "memberships",
        "default",
    )
    told = installed.stdout + installed.stderr
    assert f"Mendari is set up in {site.folder}" in told and "Mendari is up." in told
    assert "Generated now, and never again: POSTGRES_PASSWORD" in told
    secrets = [values[name] for name in install.GENERATED] + [CLIENT_SECRET]
    assert not [secret for secret in secrets if secret in told]  # never printed
    assert (site.folder / "tls" / "SELF-SIGNED.txt").exists()

    # 2. The front.
    with httpx.Client(verify=False, timeout=30) as web:
        health = web.get(f"{site.base}/api/health")
        assert health.status_code == 200 and health.json()["sign_in"] is True
        signed_out = web.get(f"{site.base}/")
        assert signed_out.status_code == 302
        assert signed_out.headers["location"] == "/auth/login?next=/"  # to sign-in, here
        plain = web.get(f"http://localhost:{site.http}/api/health?x=zq-query")
        assert plain.status_code == 308
        assert plain.headers["location"] == f"{site.base}/api/health?x=zq-query"
        other = web.get(f"{site.base}/", headers={"Host": "other.example"})
        assert other.status_code == 421  # not this deployment's name
        jobs = {"Authorization": f"Bearer {values['CRON_SECRET']}"}
        assert web.get(f"{site.base}/api/internal/checkpoint", headers=jobs).status_code == 404
        web.get(f"{site.base}/auth/callback?code=zq-code&state=zq-state")
    logged = site.docker("logs", f"{site.project}-proxy-1")
    front = logged.stdout + logged.stderr
    assert '"GET /auth/callback" 400' in front and "zq-" not in front  # the path alone

    # 3. The daily jobs ran once the engine was up.
    status = site.folder / "backups" / ".jobs-status"
    for _ in range(90):
        if status.exists():
            break
        time.sleep(2)
    assert status.read_text().strip() == "ok"
    (backup,) = sorted(site.folder.glob("backups/mendari-*.dump"))
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    ran = site.run("logs", "jobs").stdout
    for job in ("retention:", "checkpoint: the audit trail verifies", "monitor:", "backup:"):
        assert job in ran, job
    shown = site.run("status").stdout
    assert "Readiness: ready" in shown and "Daily jobs: the last run passed" in shown

    # 4. A real sign-in.
    site.start_identity_provider()
    stranger = site.sign_in(BO)
    assert stranger.get("/api/auth/me").json()["principal"]["org"] is None
    assert stranger.get("/api/system/checks").status_code == 403
    operator = site.sign_in(ADA)
    me = operator.get("/api/auth/me").json()["principal"]
    assert (me["org"], me["roles"]) == ("default", ["admin"]) and "operate" in me["permissions"]
    checks = operator.get("/api/system/checks")
    assert checks.status_code == 200
    states = {check["key"]: check["state"] for check in checks.json()["checks"]}
    in_order = ("sign_in", "public_address", "client_address", "patient_data", "sealing_key")
    for key in (*in_order, "database", "checkpoint_signing", "scheduled_jobs", "backup", "limits"):
        assert states[key] == "ok", (key, states[key])
    assert states["operators"] == "attention"  # one administrator so far
    assert checks.json()["setup"] == {"fresh": True, "finished": False}
    assert not [secret for secret in secrets if secret in checks.text]
    assert operator.get("/api/audit/verify").json()["ok"] is True  # the deployment's own view
    # The first-administrator setting gave what it had to give: the same address under
    # another subject gets nothing.
    again = site.sign_in(("ada-again", ADA[1], "Someone else"))
    assert again.get("/api/auth/me").json()["principal"]["org"] is None

    # 5. Installing again changes nothing.
    second = site.run("install", "--answers", str(site.answers))
    assert second.returncode == 0, second.stdout[-3000:] + second.stderr[-3000:]
    assert "No secret was generated" in second.stdout
    assert site.settings() == settings

    # 5b. Adopting an agent needs no file after the install: the organization's agent store is
    # on, the System view says how to name a registry (none was given here: the stack has no
    # way out), and the administrator, as the deployment's operator, allows one agent's service
    # at a public address the deployment's list does not name. Never an address inside.
    adopting = site.sign_in(ADA)
    adopting.post("/api/notice/acknowledge")
    assert (values["MENDARI_AGENT_STORE"], values["MENDARI_REGISTRY_URL"]) == ("on", "")
    assert adopting.get("/api/hub").json() == {
        "registry": None,
        "add": {"available": False, "reason": "no_registry"},
    }
    [registry] = [
        check
        for check in adopting.get("/api/system/checks").json()["checks"]
        if check["key"] == "registry"
    ]
    assert registry["state"] == "not_set" and "./mendari-site install" in registry["action"]
    [mine] = [member for member in adopting.get("/api/auth/members").json() if member["you"]]
    given = adopting.put(
        f"/api/auth/members/{mine['id']}/roles", json={"roles": ["admin", "pack_author"]}
    )
    assert given.status_code == 200, given.text
    held = adopting.get("/api/store/agents")
    assert held.status_code == 200 and held.json() == []  # the store answers its author
    connection = f"/api/connections/{DEMO}"
    as_installed = adopting.get(connection).json()
    assert as_installed["allowed_origins"] == [] and as_installed["may_allow_origin"] is True
    public = {"endpoint": "https://1.1.1.1/decide"}  # an address by its number: nothing is called
    assert adopting.put(connection, json=public).status_code == 422
    for inside in ("https://10.0.0.5/decide", "https://172.31.88.10/decide", "https://postgres/x"):
        kept_out = adopting.put(connection, json={"endpoint": inside, "allow_origin": True})
        assert kept_out.status_code == 422, (inside, kept_out.text)
    allowed = adopting.put(connection, json={**public, "allow_origin": True})
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["connection"]["origin_allowed_by"] == ADA[1]
    recorded = [item["event"] for item in adopting.get("/api/audit/administrative").json()["items"]]
    assert "agent_connection.origin_allowed" in recorded and "agent_connection.set" in recorded
    assert adopting.delete(connection).status_code == 204  # the demo agent answers as before

    # 6. Backup, then restore: the same audit trail head.
    operator = site.sign_in(ADA)
    operator.post("/api/notice/acknowledge")
    for n in range(3):
        body = {"message": {"body": f"Can I get a refill of my usual prescription? ({n})"}}
        assert operator.post(f"/api/agents/{DEMO}/decide", json=body).status_code == 200
    assert site.run("backup").returncode == 0
    newest = sorted(site.folder.glob("backups/mendari-*.dump"))[-1]
    at_backup = head(site.run("verify").stdout)
    assert at_backup["ok"] is True and at_backup["events"] > 0
    for n in range(2):
        body = {"message": {"body": f"I would like to book a visit next week. ({n})"}}
        assert operator.post(f"/api/agents/{DEMO}/decide", json=body).status_code == 200
    later = head(site.run("verify").stdout)
    assert later["events"] > at_backup["events"] and later["head"] != at_backup["head"]
    restored = site.run("restore", f"backups/{newest.name}", "--yes")
    assert restored.returncode == 0, restored.stdout[-3000:] + restored.stderr[-3000:]
    assert head(restored.stdout) == at_backup  # the trail as it was at the backup, verified
    assert "kept as mendari_before_" in restored.stdout
    holds = json.loads(site.run("holds").stdout)
    (hold,) = [h for h in holds if h["reason"] == "restored from a backup"]
    assert hold["org"] is None  # every organization, until someone lifts it
    assert site.run("lift", hold["id"]).returncode == 0
    assert json.loads(site.run("holds").stdout) == []
    kept = re.search(r"mendari_before_\d+", restored.stdout)
    assert kept is not None and site.run("forget", kept.group(0)).returncode == 0
    with httpx.Client(verify=False, timeout=30) as web:
        assert web.get(f"{site.base}/api/health").status_code == 200
    operator = site.sign_in(ADA)  # everyone signs in again
    assert operator.get("/api/system/checks").status_code == 200
    # The emergency stop, from the machine: it works with the engine stopped, and the engine
    # that starts afterwards holds every decision for a person.
    assert site.run("stop").returncode == 0
    with pytest.raises(httpx.HTTPError), httpx.Client(verify=False, timeout=5) as web:
        web.get(f"{site.base}/api/health")
    held = site.run("hold", "a drill; it's only a test")
    assert held.returncode == 0, held.stdout + held.stderr
    (drill,) = json.loads(site.run("holds").stdout)
    assert (drill["org"], drill["reason"]) == (None, "a drill; it's only a test")
    assert drill["set_by"].startswith("command line (")  # whoever ran the script
    assert site.run("start").returncode == 0
    operator = site.sign_in(ADA)
    seen = operator.get("/api/holds").json()
    assert [hold["id"] for hold in seen] == [drill["id"]]
    assert site.run("lift", drill["id"]).returncode == 0
    assert json.loads(site.run("holds").stdout) == []

    # 7. Fail closed: a secret missing from the settings.
    env = site.folder / ".env"
    env.write_text(
        "".join(line for line in settings.splitlines(True) if "MENDARI_PHI_KEY=" not in line)
    )
    env.chmod(0o600)
    refused = site.run("start")
    assert refused.returncode == 1
    assert "MENDARI_PHI_KEY" in refused.stdout + refused.stderr
    assert "nothing was started or changed" in refused.stderr
    again_install = site.run("install", "--answers", str(site.answers))
    assert again_install.returncode == 1  # never a new sealing key over the old one's data
    assert "no longer holds MENDARI_PHI_KEY" in again_install.stdout + again_install.stderr
    with httpx.Client(verify=False, timeout=30) as web:
        assert web.get(f"{site.base}/api/health").status_code == 200  # still serving
    env.write_text(settings)
    env.chmod(0o600)
    assert site.run("start").returncode == 0

    # 8. An upgrade to a new build, and back.
    upgraded = site.run("upgrade", "--source", str(newer_source(site.folder.parent)))
    assert upgraded.returncode == 0, upgraded.stdout[-3000:] + upgraded.stderr[-3000:]
    steps = [
        "1 of 6. The audit trail, checked with the new version, read-only",
        "no fork in the audit trail",
        "2 of 6. The settings, read by the new version",
        "3 of 6. A backup, before anything changes",
        "4 of 6. The database, brought up to date by the new version",
        "5 of 6. Recording the new version",
        "6 of 6. Restarting on the new version",
        "Upgraded to mendari-engine:site-",
        "./mendari-site rollback",
    ]
    places = [upgraded.stdout.index(step) for step in steps]
    assert places == sorted(places)  # each step, in order
    assert head(upgraded.stdout)["ok"] is True
    now = install.parse(site.settings())
    assert now["MENDARI_IMAGE"] != values["MENDARI_IMAGE"]
    assert now["MENDARI_IMAGE_PREVIOUS"] == values["MENDARI_IMAGE"]
    assert {k: v for k, v in now.items() if not k.startswith("MENDARI_IMAGE")} == {
        k: v for k, v in values.items() if not k.startswith("MENDARI_IMAGE")
    }  # no secret and no answer changed
    with httpx.Client(verify=False, timeout=30) as web:
        assert web.get(f"{site.base}/api/health").status_code == 200
    running = site.docker("inspect", "--format", "{{.Config.Image}}", f"{site.project}-engine-1")
    assert running.stdout.strip() == now["MENDARI_IMAGE"]
    back = site.run("rollback")
    assert back.returncode == 0, back.stdout[-3000:] + back.stderr[-3000:]
    assert install.parse(site.settings())["MENDARI_IMAGE"] == values["MENDARI_IMAGE"]
    with httpx.Client(verify=False, timeout=30) as web:
        assert web.get(f"{site.base}/api/health").status_code == 200
    assert head(site.run("verify").stdout)["ok"] is True  # the trail holds through it all


def newer_source(folder: Path) -> Path:
    """A copy of what the image is built from, with one line added to the README: the next
    version, as far as an image's identity goes."""
    newer = folder / "newer"
    newer.mkdir()
    for name in ("Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock", "LICENSE", "NOTICE"):
        shutil.copy(ROOT / name, newer / name)
    (newer / "README.md").write_text((ROOT / "README.md").read_text() + "\nThe next build.\n")
    caches = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(ROOT / "src", newer / "src", ignore=caches)
    shutil.copytree(ROOT / "agents", newer / "agents")
    (newer / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "image_notices.py", newer / "scripts" / "image_notices.py")
    shutil.copytree(ROOT / "licenses" / "image", newer / "licenses" / "image")
    return newer
