"""传输路由解析与命令构造的纯函数单测（无 IO、无子进程）。

覆盖：resolve_route 全场景表驱动、validate_rsync_args 白名单、
classify_transfer_failure 真实 stderr 样本分类、PROGRESS2_RE 进度行解析、
build_bridged_command argv 金样与脱敏断言。
"""

from __future__ import annotations

import shlex

import pytest

from exp_scheduler_app.nodes import ResolvedAuth
from exp_scheduler_app.transfer import (
    BRIDGE_SENTINEL,
    EXIT_NO_AGENT,
    EXIT_NO_MKTEMP,
    PARTIAL_DIR,
    PROGRESS2_RE,
    build_bridged_command,
    classify_transfer_failure,
    parse_progress_line,
    resolve_route,
    validate_rsync_args,
)


def _cand(resolution: dict[str, object], route: str) -> dict[str, object]:
    return next(c for c in resolution["candidates"] if c["route"] == route)  # type: ignore[union-attr]


def _routes(resolution: dict[str, object]) -> set[str]:
    return {str(c["route"]) for c in resolution["candidates"]}  # type: ignore[union-attr]


# ---------- resolve_route ----------


@pytest.mark.parametrize(
    ("src", "dst", "links", "auths", "expected_route", "expected_needs_probe"),
    [
        # 本机到本机
        ("local", "local", {}, {}, "local", False),
        # local → remote：直连推送
        ("local", "b", {("local", "b"): "ok"}, {"b": "key"}, "direct_from_src", False),
        # remote → local：直连拉取
        ("a", "local", {("local", "a"): "ok"}, {"a": "key"}, "direct_from_dst", False),
        # A → B 直连边 ok：优先 direct_from_src
        (
            "a",
            "b",
            {("local", "a"): "ok", ("a", "b"): "ok"},
            {"a": "key", "b": "key"},
            "direct_from_src",
            False,
        ),
        # A → B 直连双向 failed，但主控双向可达：降到 bridged_push
        (
            "a",
            "b",
            {
                ("local", "a"): "ok",
                ("local", "b"): "ok",
                ("a", "b"): "failed",
                ("b", "a"): "failed",
            },
            {"a": "key", "b": "key"},
            "bridged_push",
            False,
        ),
        # dst 为密码认证：bridged_push 不可行，回落 bridged_pull（src 为 key）
        (
            "a",
            "b",
            {
                ("local", "a"): "ok",
                ("local", "b"): "ok",
                ("a", "b"): "failed",
                ("b", "a"): "failed",
            },
            {"a": "key", "b": "password"},
            "bridged_pull",
            False,
        ),
        # 双端密码：没有任何可行候选
        ("a", "b", {("local", "a"): "ok", ("local", "b"): "ok"},
         {"a": "password", "b": "password"}, None, False),
        # 全 unknown 边：推荐 direct_from_src 但需要探测
        ("a", "b", {}, {"a": "key", "b": "key"}, "direct_from_src", True),
    ],
)
def test_resolve_route_table(src, dst, links, auths, expected_route, expected_needs_probe):
    resolution = resolve_route(src, dst, links, auths)
    assert resolution["recommended"] == expected_route
    assert resolution["needs_probe"] is expected_needs_probe


def test_resolve_route_unknown_edges_requires_probe_content():
    resolution = resolve_route("a", "b", {}, {"a": "key", "b": "key"})
    assert resolution["needs_probe"] is True
    cand = _cand(resolution, "direct_from_src")
    assert cand["feasible"] is True
    # 未知边按 (from, to) 顺序列出：先主控→发起端，再发起端→对端
    assert cand["requires_probe"] == [["local", "a"], ["a", "b"]]
    # 桥接候选只依赖主控出边
    bridged = _cand(resolution, "bridged_push")
    assert bridged["requires_probe"] == [["local", "a"], ["local", "b"]]


def test_resolve_route_password_dst_blocks_push_candidates():
    links = {
        ("local", "a"): "ok",
        ("local", "b"): "ok",
        ("a", "b"): "failed",
        ("b", "a"): "failed",
    }
    resolution = resolve_route("a", "b", links, {"a": "key", "b": "password"})
    push = _cand(resolution, "bridged_push")
    assert push["feasible"] is False
    assert any("密钥认证" in str(r) for r in push["reasons"])
    direct = _cand(resolution, "direct_from_src")
    assert direct["feasible"] is False
    assert any("密码认证" in str(r) for r in direct["reasons"])
    pull = _cand(resolution, "bridged_pull")
    assert pull["feasible"] is True


def test_resolve_route_double_password_has_no_feasible_candidate():
    resolution = resolve_route(
        "a",
        "b",
        {("local", "a"): "ok", ("local", "b"): "ok"},
        {"a": "password", "b": "password"},
    )
    assert resolution["recommended"] is None
    assert all(not c["feasible"] for c in resolution["candidates"])  # type: ignore[union-attr]
    # 每个候选都有中文原因可供 409 提示
    assert all(c["reasons"] for c in resolution["candidates"])  # type: ignore[union-attr]


def test_resolve_route_failed_edge_reason_mentions_link():
    resolution = resolve_route(
        "a",
        "b",
        {("local", "a"): "ok", ("a", "b"): "failed"},
        {"a": "key", "b": "key"},
    )
    cand = _cand(resolution, "direct_from_src")
    assert cand["feasible"] is False
    assert any("a → b" in str(r) and "失败" in str(r) for r in cand["reasons"])
    # failed 的候选不再要求探测
    assert cand["requires_probe"] == []


def test_resolve_route_manual_candidate_sets():
    """手动路由校验依据：候选集合与可行性（create_job 手动模式按此拒绝）。"""
    # local→remote：只有两个 direct 候选，桥接不适用（手动选 bridged_push 应判"不适用"）
    resolution = resolve_route("local", "b", {("local", "b"): "ok"}, {"b": "key"})
    assert _routes(resolution) == {"direct_from_src", "direct_from_dst"}
    reverse = _cand(resolution, "direct_from_dst")
    assert reverse["feasible"] is False
    assert any("本机不能作为远端连入目标" in str(r) for r in reverse["reasons"])

    # remote→remote：四个候选齐全
    resolution = resolve_route("a", "b", {}, {"a": "key", "b": "key"})
    assert _routes(resolution) == {
        "direct_from_src",
        "direct_from_dst",
        "bridged_push",
        "bridged_pull",
    }

    # local→local：只有 local 候选
    resolution = resolve_route("local", "local", {}, {})
    assert _routes(resolution) == {"local"}
    assert _cand(resolution, "local")["feasible"] is True


# ---------- validate_rsync_args ----------


def test_validate_rsync_args_accepts_whitelisted():
    args = [
        "-z",
        "--compress",
        "--exclude=*.ckpt",
        "--include=keep/**",
        "--bwlimit=20000",
        "--checksum",
        "--update",
        "--ignore-existing",
        "--no-perms",
        "--chmod=D755,F644",
        "  ",
        "",
    ]
    assert validate_rsync_args(args) == [
        "-z",
        "--compress",
        "--exclude=*.ckpt",
        "--include=keep/**",
        "--bwlimit=20000",
        "--checksum",
        "--update",
        "--ignore-existing",
        "--no-perms",
        "--chmod=D755,F644",
    ]


@pytest.mark.parametrize(
    "bad",
    [
        "-e",  # 改变远端 shell，注入面
        "--rsh=x",
        "--rsync-path=x",
        "--inplace",  # 截断文件正确性陷阱
        "--delete",  # 走独立 flag，不允许从 rsync_args 混入
        "a;b",  # shell 注入形
        "--exclude=a;b",  # 值中带 ';' 注入形
        "--exclude",  # 裸写会吞掉下一个 argv（路径）
        "--out-format=%i",
    ],
)
def test_validate_rsync_args_rejects(bad):
    with pytest.raises(ValueError):
        validate_rsync_args([bad])


# ---------- classify_transfer_failure ----------


def test_classify_forward_failed_retries_with_new_port():
    code, message, retry = classify_transfer_failure(
        "connecting",
        255,
        "Error: remote port forwarding failed for listen port 23456",
        23456,
    )
    assert code == "bridge_forward_denied"
    assert retry is True
    assert "23456" in message


def test_classify_via_auth_failed_in_connecting_phase():
    code, _message, retry = classify_transfer_failure(
        "connecting",
        255,
        "ubuntu@10.0.0.5: Permission denied (publickey,password).",
        None,
    )
    assert code == "via_auth_failed"
    assert retry is False


def test_classify_exit_97_agent_forward_disabled():
    code, message, retry = classify_transfer_failure("connecting", EXIT_NO_AGENT, "", 23456)
    assert code == "via_agent_forward_disabled"
    assert retry is False
    assert "agent" in message


def test_classify_rsync_partial_code_23():
    stderr = (
        'rsync: [sender] read errors mapping "/data/x": file has vanished\n'
        "rsync error: some files/attrs were not transferred "
        "(see previous errors) (code 23) at main.c(1338) [sender=3.2.7]"
    )
    code, message, retry = classify_transfer_failure("transferring", 23, stderr, None)
    assert code == "rsync_partial"
    assert retry is False
    assert "部分文件" in message


def test_classify_dst_disk_full():
    stderr = 'rsync: [receiver] write failed on "/data/model.bin": No space left on device (28)'
    code, message, retry = classify_transfer_failure("transferring", 11, stderr, None)
    assert code == "dst_disk_full"
    assert retry is False
    assert "磁盘空间不足" in message


# ---------- PROGRESS2_RE ----------


def test_progress_line_with_to_chk():
    parsed = parse_progress_line(
        "  1,234,567,890  45%  103.25MB/s    0:01:23 (xfr#12, to-chk=345/678)"
    )
    assert parsed == {
        "bytes": 1_234_567_890,
        "percent": 45,
        "rate": "103.25MB/s",
        "eta": "0:01:23",
        "xfr": 12,
    }


def test_progress_line_with_ir_chk():
    parsed = parse_progress_line(
        "      3,211,264   1%  306.15kB/s    0:18:24 (xfr#5, ir-chk=1024/2048)"
    )
    assert parsed is not None
    assert parsed["bytes"] == 3_211_264
    assert parsed["percent"] == 1
    assert parsed["xfr"] == 5


def test_progress_line_without_xfr_segment():
    parsed = parse_progress_line("         12,345   0%    0.00kB/s    0:00:00")
    assert parsed is not None
    assert parsed["bytes"] == 12_345
    assert parsed["percent"] == 0
    assert parsed["xfr"] is None


def test_progress_regex_rejects_non_progress_lines():
    assert parse_progress_line("sending incremental file list") is None
    assert PROGRESS2_RE.match("checkpoints/model-00001-of-00002.safetensors") is None


# ---------- build_bridged_command ----------


def _remote_auth(
    node_id: str,
    *,
    method: str = "key",
    host: str = "10.0.0.5",
    port: int = 2200,
    username: str = "ubuntu",
    password: str | None = None,
) -> ResolvedAuth:
    return ResolvedAuth(
        node_id=node_id,
        name=f"节点{node_id}",
        is_local=False,
        host=host,
        port=port,
        username=username,
        auth_method=method,
        key_path=f"/keys/{node_id}" if method == "key" else None,
        password=password,
    )


def _build(via: ResolvedAuth, **overrides):
    peer = _remote_auth("b", host="10.0.0.6", port=2222, username="worker")
    params = {
        "via": via,
        "peer": peer,
        "direction": "push",
        "src_path": "/data/src/",
        "dst_path": "/data/dst",
        "listen_port": 23456,
        "peer_hostkey_lines": ["expsched-b ssh-ed25519 AAAAC3FAKEKEY"],
        "known_hosts_path": "/state/known_hosts",
        "agent_sock": "/state/run/agent-j.sock",
        "job_id": "job1",
    }
    params.update(overrides)
    return build_bridged_command(**params)


def test_build_bridged_command_golden_argv():
    cmd = _build(_remote_auth("a"))
    argv = cmd.argv
    # 密钥认证不经 sshpass
    assert argv[0] == "ssh"
    assert "-A" in argv and "-T" in argv
    # -R 串：主控本地回环监听端口 → peer 真实地址
    r_index = argv.index("-R")
    assert argv[r_index + 1] == "127.0.0.1:23456:10.0.0.6:2222"
    # ExitOnForwardFailure 保证转发失败时远程命令不执行，换端口重试安全
    forward_index = argv.index("ExitOnForwardFailure=yes")
    assert argv[forward_index - 1] == "-o"
    # 远端命令是单个 argv 元素：sh -c <quoted snippet>
    assert argv[-3] == "--"
    assert argv[-2] == "ubuntu@10.0.0.5"
    remote_command = argv[-1]
    assert remote_command.startswith("sh -c ")
    words = shlex.split(remote_command)
    assert words[:2] == ["sh", "-c"]
    assert len(words) == 3
    snippet = words[2]
    # 哨兵 + partial-dir + hostkey 钉死 + agent 前置检查
    assert BRIDGE_SENTINEL in snippet
    assert f"--partial-dir={PARTIAL_DIR}" in snippet
    assert f"kh=$(mktemp) || exit {EXIT_NO_MKTEMP}" in snippet
    assert "HostKeyAlias=expsched-b" in snippet
    assert "StrictHostKeyChecking=yes" in snippet
    assert "-p 23456" in snippet
    assert "worker@127.0.0.1:/data/dst" in snippet
    assert f"exit {EXIT_NO_AGENT}" in snippet
    # env 与端口记录
    assert cmd.env_extra["SSH_AUTH_SOCK"] == "/state/run/agent-j.sock"
    assert cmd.env_extra["EXPSCHED_JOB_ID"] == "job1"
    assert "SSHPASS" not in cmd.env_extra
    assert cmd.listen_port == 23456


def test_build_bridged_command_password_via_redacted():
    secret = "S3cret!pw"
    cmd = _build(_remote_auth("a", method="password", password=secret))
    assert cmd.argv[0] == "sshpass"
    assert cmd.argv[1] == "-e"
    assert cmd.argv[2] == "ssh"
    # 密码只经 env，绝不进 argv / redacted
    assert cmd.env_extra["SSHPASS"] == secret
    assert all(secret not in part for part in cmd.argv)
    assert secret not in cmd.redacted
    assert cmd.redacted.startswith("sshpass -e")
    # 密码模式外层 ssh 不能加 BatchMode（仅 snippet 内的 inner ssh 有）
    assert "BatchMode=yes" not in cmd.argv[:-1]


def test_build_bridged_command_requires_peer_hostkey():
    with pytest.raises(ValueError, match="主机密钥"):
        _build(_remote_auth("a"), peer_hostkey_lines=[])
