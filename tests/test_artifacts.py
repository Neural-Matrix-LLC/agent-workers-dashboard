"""File artifacts (design M4 section 15.5: A-01 to A-11)."""

import base64
import hashlib

import pytest
from taskkit import fails, ok, task

from agent_hub.config import AgentConfig
from agent_hub.connectors.claude_code import ClaudeCodeConnector, safety_settings
from agent_hub.core import artifact_rules

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
PDF = b"%PDF-1.7\n" + b"x" * 40
ZIP = b"PK\x03\x04" + b"\x00\xff" * 40


async def attach(env, task_id, name, content: bytes | str, agent="hermes", **args):
    if isinstance(content, bytes):
        args.update(content=base64.b64encode(content).decode(), encoding="base64")
    else:
        args["content"] = content
    return await ok(env, agent, "attach_artifact", task_id=task_id, name=name, **args)


async def upload(env, task_id, name, data: bytes, agent="hermes", **fields):
    return await env.http.post(f"/agent/v1/tasks/{task_id}/artifacts", headers=env.auth(agent),
                               files={"file": (name, data, fields.pop("content_type", "application/octet-stream"))},
                               data=fields)


def store_files(env) -> list:
    root = env.hub.task_service.store.root
    return [p for p in root.rglob("*") if p.is_file() and p.parent.name != "tmp"]


async def test_a01_upload_and_download_round_trip(hub_env):
    async with hub_env() as env:
        view = await task(env, assignee="hermes")
        text = await attach(env, view["id"], "report.txt", "line one\nline two\n", description="Full run")
        binary = await attach(env, view["id"], "shot.png", PNG)
        response = await upload(env, view["id"], "big.csv", b"a,b\n1,2\n", content_type="text/csv")
        assert response.status_code == 201
        uploaded = response.json()["artifact"]
        assert (text["artifact"]["media_type"], binary["artifact"]["media_type"], uploaded["media_type"]) == \
            ("text/plain", "image/png", "text/csv")
        for artifact, expected in ((text["artifact"], b"line one\nline two\n"), (binary["artifact"], PNG),
                                   (uploaded, b"a,b\n1,2\n")):
            download = await env.get("claude", f"/artifacts/{artifact['id']}/content")
            assert download.status_code == 200 and download.content == expected
            assert download.headers["content-disposition"].startswith("attachment; filename*=UTF-8''")
            assert download.headers["x-content-type-options"] == "nosniff"
            assert download.headers["content-security-policy"] == "default-src 'none'; sandbox"
            assert download.headers["cache-control"] == "private, no-store"
        assert (await env.get("claude", f"/artifacts/{text['artifact']['id']}/content")).headers["content-type"] == \
            "text/plain; charset=utf-8"
        ranged = await env.http.get(f"/agent/v1/artifacts/{binary['artifact']['id']}/content",
                                    headers={**env.auth("claude"), "Range": "bytes=0-7"})
        assert ranged.status_code == 206 and ranged.content == PNG[:8]
        assert ranged.headers["content-range"] == f"bytes 0-7/{len(PNG)}"
        meta = (await env.get("claude", f"/artifacts/{text['artifact']['id']}")).json()
        assert meta["description"] == "Full run" and meta["task_ref"] == "room:main#1"
        assert (await ok(env, "claude", "get_task", task_id=view["id"]))["artifact_count"] == 3


async def test_a02_same_content_is_stored_once(hub_env):
    async with hub_env() as env:
        view = await task(env, assignee="hermes")
        first = await attach(env, view["id"], "a.txt", "same bytes")
        second = await attach(env, view["id"], "b.txt", "same bytes")
        assert first["artifact"]["id"] != second["artifact"]["id"]
        assert first["artifact"]["sha256"] == second["artifact"]["sha256"]
        assert len(store_files(env)) == 1


async def test_a03_size_limits_and_quotas(hub_env):
    limits = {"artifacts": {"max_inline_bytes": 10, "max_bytes": 20, "max_task_bytes": 30, "max_per_task": 3}}
    async with hub_env(limits=limits) as env:
        view = await task(env, assignee="hermes")
        message = await fails(env, "hermes", "attach_artifact", "too_large", task_id=view["id"], name="a.txt",
                              content="x" * 11)
        assert "HTTP route" in message
        too_big = await upload(env, view["id"], "b.txt", b"x" * 21)
        assert too_big.status_code == 413 and too_big.json()["error"] == "too_large"
        assert list((env.hub.task_service.store.root / "tmp").iterdir()) == []      # nothing left behind
        assert (await upload(env, view["id"], "c.txt", b"y" * 20)).status_code == 201
        quota = await upload(env, view["id"], "d.txt", b"z" * 11)
        assert quota.status_code == 413 and quota.json()["error"] == "quota_exceeded"
        await attach(env, view["id"], "e.txt", "small")                   # 25 of 30 bytes
        await attach(env, view["id"], "f.txt", "s")                       # the third file
        await fails(env, "hermes", "attach_artifact", "quota_exceeded", task_id=view["id"], name="g.txt", content="s")


async def test_a03_store_quota(hub_env):
    async with hub_env(limits={"artifacts": {"max_store_bytes": 10}}) as env:
        view = await task(env, assignee="hermes")
        await attach(env, view["id"], "a.txt", "12345678")
        await fails(env, "hermes", "attach_artifact", "quota_exceeded", task_id=view["id"], name="b.txt",
                    content="different!")
        await attach(env, view["id"], "c.txt", "12345678")                # the same content takes no space
        notices = [m.body for m in await env.hub.repo.messages(env.hub.owner, "room:main", limit=500)
                   if m.kind == "notice"]
        assert any("Artifact store is full" in n for n in notices)


async def test_a04_types_are_sniffed_not_trusted(hub_env):
    async with hub_env() as env:
        view = await task(env, assignee="hermes")
        png = await attach(env, view["id"], "notes.txt", PNG, media_type="text/plain")
        assert png["artifact"]["media_type"] == "image/png"
        html = await attach(env, view["id"], "page.html", "<script>alert(1)</script>", media_type="text/html")
        assert html["artifact"]["media_type"] == "text/plain"              # shown as source, never rendered
        await fails(env, "hermes", "attach_artifact", "unsupported_media_type", task_id=view["id"],
                    name="archive.zip", content=base64.b64encode(ZIP).decode(), encoding="base64")
        bad_json = await attach(env, view["id"], "data.json", "{not json", media_type="application/json")
        assert bad_json["artifact"]["media_type"] == "text/plain"
        good = await attach(env, view["id"], "data.json", '{"ok": true}')
        assert good["artifact"]["media_type"] == "application/json"
        assert (await attach(env, view["id"], "doc.pdf", PDF))["artifact"]["media_type"] == "application/pdf"
        await fails(env, "hermes", "attach_artifact", "bad_encoding", task_id=view["id"], name="x.bin",
                    content="%%%not base64", encoding="base64")


SECRETS = {
    "private-key": "-----BEGIN OPENSSH PRIVATE KEY-----",
    "aws-access-key": "key AKIAABCDEFGHIJKLMNOP here",
    "github-token": "ghp_" + "a" * 36,
    "slack-token": "xoxb-1234567890-abcdef",
    "anthropic-key": "sk-ant-" + "b" * 30,
    "openai-key": "sk-" + "c" * 30,
    "xai-key": "xai-" + "d" * 30,
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlMTIz",
    "bearer-token": "Authorization: Bearer " + "e" * 32,
    "secret-assignment": "HUB_GROK_TOKEN=abc123",
}


@pytest.mark.parametrize("rule", SECRETS)
def test_a05_each_pattern_family(rule):
    assert artifact_rules.scan(f"first line\n{SECRETS[rule]}\n") == (rule, 2)


def test_a05_placeholders_and_plain_text_pass():
    for text in ("HUB_GROK_TOKEN=<token>", "set PASSWORD=$PW", "the word token", "sk-short", "API_KEY="):
        assert artifact_rules.scan(text) is None, text


async def test_a05_secrets_are_refused_without_echoing_them(hub_env, monkeypatch):
    real_token = "f" * 64                                                 # like `openssl rand -hex 32`
    monkeypatch.setenv("HUB_CLAUDE_TOKEN", real_token)
    async with hub_env() as env:
        view = await task(env, agent="hermes", assignee="hermes")
        message = await fails(env, "hermes", "attach_artifact", "secret_detected", task_id=view["id"],
                              name="log.txt", content="ok\nAKIAABCDEFGHIJKLMNOP\n")
        assert "aws-access-key" in message and "line 2" in message and "AKIA" not in message
        # A real agent token from this hub is matched by its hash.
        message = await fails(env, "hermes", "attach_artifact", "secret_detected", task_id=view["id"],
                              name="t.txt", content=f"claude's token is {real_token}")
        assert "hub-agent-token" in message
        notices = [m.body for m in await env.hub.repo.messages(env.hub.owner, "room:main", limit=500)
                   if m.kind == "notice"]
        assert "hermes tried to attach a file with a secret to room:main#1 (rule: aws-access-key). It was " \
               "refused." in notices
        # Task text is scanned too.
        await fails(env, "hermes", "update_task", "secret_detected", task_id=view["id"], action="note",
                    note="use ghp_" + "z" * 36)
        assert store_files(env) == []


def test_a05_token_match_by_hash():
    token = "a" * 64
    assert artifact_rules.scan(f"x {token} y", {hashlib.sha256(token.encode()).hexdigest()}) == \
        ("hub-agent-token", 1)


@pytest.mark.parametrize("name", [".env", ".env.local", "server.pem", "api.key", "id_rsa", "id_ed25519.pub",
                                  "cert.p12", "credentials.json", "vault.kdbx"])
async def test_a06_forbidden_file_names(hub_env, name):
    async with hub_env() as env:
        view = await task(env, assignee="hermes")
        await fails(env, "hermes", "attach_artifact", "secret_detected", task_id=view["id"], name=name, content="x")


async def test_a07_names_are_metadata_only(hub_env):
    async with hub_env() as env:
        view = await task(env, assignee="hermes")
        result = await attach(env, view["id"], "../../etc/passwd", "root:x:0:0")
        assert result["artifact"]["name"] == "passwd"
        [stored] = store_files(env)
        assert stored.name == result["artifact"]["sha256"] and stored.parent.parent == env.hub.task_service.store.root
    assert artifact_rules.sanitize_name("..\\..\\x\u0000y.txt") == "xy.txt"
    assert artifact_rules.sanitize_name("...") == "artifact"
    assert len(artifact_rules.sanitize_name("a" * 300)) == 120


async def test_a08_supersedes_must_be_on_the_same_task(hub_env):
    async with hub_env() as env:
        first = await task(env, assignee="hermes")
        other = await task(env, title="other", assignee="hermes")
        old = await attach(env, first["id"], "r.txt", "v1")
        new = await attach(env, first["id"], "r.txt", "v2", supersedes=old["artifact"]["id"])
        assert new["artifact"]["supersedes"] == old["artifact"]["id"]
        await fails(env, "hermes", "attach_artifact", "bad_supersedes", task_id=other["id"], name="r.txt",
                    content="v3", supersedes=old["artifact"]["id"])


async def test_a09_owner_soft_delete_and_gc(hub_env):
    async with hub_env() as env:
        await env.login()
        view = await task(env, assignee="hermes")
        keep = await attach(env, view["id"], "a.txt", "shared")
        twin = await attach(env, view["id"], "b.txt", "shared")
        alone = await attach(env, view["id"], "c.txt", "only me")
        assert len(store_files(env)) == 2
        assert (await env.http.delete(f"/api/artifacts/{twin['artifact']['id']}")).json() == {"ok": True}
        assert len(store_files(env)) == 2                                  # still used by `keep`
        await fails(env, "claude", "read_artifact", "unknown_artifact", artifact_id=twin["artifact"]["id"])
        assert (await env.get("claude", f"/artifacts/{twin['artifact']['id']}/content")).status_code == 404
        owner_meta = (await env.http.get(f"/api/artifacts/{twin['artifact']['id']}")).json()
        assert owner_meta["deleted_at"]
        await env.http.delete(f"/api/artifacts/{alone['artifact']['id']}")
        assert len(store_files(env)) == 1                                  # unreferenced file removed
        assert (await ok(env, "claude", "read_artifact", artifact_id=keep["artifact"]["id"]))["content"] == "shared"
        assert (await env.http.delete(f"/api/artifacts/{alone['artifact']['id']}")).status_code == 404
        assert any("removed by the owner" in m.body for m in await env.hub.repo.messages(env.hub.owner, "room:main"))


async def test_a10_read_artifact_pages(hub_env):
    async with hub_env() as env:
        view = await task(env, assignee="hermes")
        text = await attach(env, view["id"], "t.txt", "héllo wörld")
        first = await ok(env, "claude", "read_artifact", artifact_id=text["artifact"]["id"], limit=5)
        assert (first["encoding"], first["content"], first["next_offset"]) == ("utf8", "héllo", 5)
        rest = await ok(env, "claude", "read_artifact", artifact_id=text["artifact"]["id"], offset=5)
        assert (rest["content"], rest["next_offset"]) == (" wörld", None)
        binary = await attach(env, view["id"], "p.png", PNG)
        chunk = await ok(env, "claude", "read_artifact", artifact_id=binary["artifact"]["id"], limit=8)
        assert chunk["encoding"] == "base64" and base64.b64decode(chunk["content"]) == PNG[:8]
        assert chunk["next_offset"] == 8
        await fails(env, "claude", "read_artifact", "bad_field", artifact_id=binary["artifact"]["id"], offset=-1)


def test_a11_claude_cannot_reach_the_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_STATE_DIR", str(tmp_path / "state"))
    connector = ClaudeCodeConnector(AgentConfig(id="claude", connector="claude-code", workdir=str(tmp_path),
                                                command="true", sandbox=False), "http://hub", "t")
    connector.extra_protected.append("/srv/hub-data/artifacts/**")
    protected = connector._protected()
    assert any(p.endswith("/artifacts/**") and "/srv/" not in p for p in protected)    # the default location
    deny = safety_settings({}, protected)["permissions"]["deny"]
    assert "Read(//srv/hub-data/artifacts/**)" in deny and "Edit(//srv/hub-data/artifacts/**)" in deny


async def test_attach_permissions(hub_env):
    async with hub_env() as env:
        view = await task(env, assignee="hermes")
        await fails(env, "grok", "attach_artifact", "not_allowed", task_id=view["id"], name="x.txt", content="x")
        await ok(env, "claude", "send_dm", to="hermes", body="psst")
        hidden = await task(env, conversation="dm:claude:hermes", title="hidden", assignee="hermes")
        await fails(env, "grok", "attach_artifact", "unknown_task", task_id=hidden["id"], name="x.txt", content="x")


async def test_malformed_json_upload_is_a_400(hub_env):
    async with hub_env() as env:
        view = await task(env, assignee="hermes")
        for body in (b"{not json", b'{"name": "x.txt"}'):
            response = await env.http.post(f"/agent/v1/tasks/{view['id']}/artifacts", content=body,
                                           headers={**env.auth("hermes"), "content-type": "application/json"})
            assert response.status_code == 400 and response.json()["error"] == "bad_field"
        missing = await env.http.post(f"/agent/v1/tasks/{view['id']}/artifacts", files={"other": ("a", b"x")},
                                      headers=env.auth("hermes"))
        assert missing.json()["error"] == "missing_field"
