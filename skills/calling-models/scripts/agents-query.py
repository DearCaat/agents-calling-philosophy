#!/usr/bin/env python3
"""Query the current model entry table in the private agents-local repository."""

import argparse
import locale
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import unicodedata


REMOTE = "git@github.com:DearCaat/agents-local.git"
REMOTE_HTTPS = "https://github.com/DearCaat/agents-local.git"  # SSH 不通的机器（只配了 https Git 认证）
SOURCE = "https://github.com/DearCaat/agents-local/blob"
TABLES = {
    "entries": "local/entries.md",
    "routes": "local/api-routes.tsv",
    "harnesses": "local/harnesses.md",
}


class MissingFile(RuntimeError):
    """The fetched commit has no file at the requested path (checked with git ls-tree)."""


def _kill_tree(process):
    """Kill the process and everything it started (its process group, or its tree on Windows), then reap it.
    Bounded to about one second in total, so that the next transport still gets most of its share."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=0.7)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=0.3)
    except subprocess.TimeoutExpired:
        pass


def _newlines(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _run(args, *, timeout, env, check=True, encoding="utf-8"):
    """subprocess.run for Git steps. Output goes to temporary files, not pipes, so a descendant that keeps a
    handle open cannot hold the caller past the timeout; on timeout or interrupt (Ctrl-C) the whole process
    tree is killed. Output is decoded only after the command finished, so a timeout is always reported as
    TimeoutExpired. Returns CompletedProcess with text stdout/stderr."""
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(args, env=env, stdout=out, stderr=err, **group)
        try:
            process.wait(timeout=timeout)
        except BaseException:  # TimeoutExpired, KeyboardInterrupt
            _kill_tree(process)
            raise
        out.seek(0)
        err.seek(0)
        # same newline handling as text=True (universal newlines): \r\n and \r become \n
        stdout = _newlines(out.read().decode(encoding))
        stderr = _newlines(err.read().decode(encoding, "replace"))
    if check and process.returncode:
        raise subprocess.CalledProcessError(process.returncode, args, stdout, stderr)
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def read_table(path, deadline=None):
    """Return (commit, text). deadline is a time.monotonic() value bounding all Git steps together;
    SSH may use at most half of what is left, so https still gets a chance when SSH hangs."""
    import time

    def left(share=1.0):
        if deadline is None:
            return 20
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise subprocess.TimeoutExpired("git", 0)
        return min(20, remain * share)

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    ssh_config = next(
        (
            Path(env[key]) / ".git/config"
            for key in ("AGENTS_DATA_ROOT", "CLAUDE_PLUGIN_DATA", "PLUGIN_DATA", "GROK_PLUGIN_DATA")
            if env.get(key)
        ),
        None,
    )
    if "GIT_SSH_COMMAND" not in env and "GIT_SSH" not in env and ssh_config and ssh_config.is_file():
        # Reuse the configured Git authentication without opening the local inventory.
        ssh = _run(
            ["git", "config", "--file", str(ssh_config), "--get", "core.sshCommand"],
            env=env, timeout=min(5, left()), check=False,
            encoding=locale.getpreferredencoding(False),  # git config prints values in the local encoding
        )
        if ssh.returncode == 0 and ssh.stdout.strip():
            env["GIT_SSH_COMMAND"] = ssh.stdout.strip()
    if "GIT_SSH" not in env:
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=5")

    with tempfile.TemporaryDirectory(prefix="agents-query-") as directory:
        git = ["git", "--git-dir", directory]
        _run(git + ["init", "--bare", "--quiet"], env=env, timeout=left())
        try:
            _run(git + ["fetch", "--quiet", "--depth=1", "--no-tags", REMOTE, "refs/heads/main"],
                 env=env, timeout=left(0.5))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            _run(git + ["fetch", "--quiet", "--depth=1", "--no-tags", REMOTE_HTTPS, "refs/heads/main"],
                 env=env, timeout=left())
        commit = _run(git + ["rev-parse", "FETCH_HEAD"], env=env, timeout=left()).stdout.strip()
        listed = _run(git + ["ls-tree", "--name-only", commit, "--", path], env=env, timeout=left()).stdout
        if not listed.strip():
            raise MissingFile(f"该提交里没有这个文件。\n{SOURCE}/{commit}/{path}")
        try:
            table = _run(git + ["show", f"{commit}:{path}"], env=env, timeout=left()).stdout
        except subprocess.CalledProcessError:
            raise RuntimeError(f"无法读取该提交中的文件。\n{SOURCE}/{commit}/{path}") from None
    return commit, table


def normalize(value):
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    return re.sub(r"[\s-]+", " ", value)


def matching_entries(table, query):
    heading = ""
    fields = []
    has_entries = False
    for number, line in enumerate(table.splitlines(), 1):
        if line.startswith("## "):
            heading = line.removeprefix("## ")
            fields = []
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip("|"))]
        if cells[0] == "说法":
            fields = cells
            continue
        if not fields or re.fullmatch(r"[:\-]+", cells[0]):
            continue
        aliases = [quoted or code for quoted, code in re.findall(r"「([^」]+)」|`([^`]+)`", cells[0])]
        if len(cells) != len(fields) or not aliases:
            raise RuntimeError(f"入口表第 {number} 行格式无法识别。")
        has_entries = True
        if query in {normalize(alias) for alias in aliases}:
            yield number, heading, dict(zip(fields, cells))
    if not has_entries:
        raise RuntimeError("入口表缺少可识别的登记行。")


def matching_pending_notes(table, query):
    in_pending_section = False
    for number, line in enumerate(table.splitlines(), 1):
        if line.startswith("## "):
            in_pending_section = line == "## 待核"
        if not in_pending_section or not line.startswith("- "):
            continue
        label, separator, detail = line[2:].partition("：")
        if separator and normalize(label.removesuffix(" 裸名").strip("`「」")) == query:
            yield number, detail.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ask = commands.add_parser("ask", help="look up a registered name or alias")
    ask.add_argument("name")
    show = commands.add_parser("show", help="read a reference table")
    show.add_argument("table", choices=TABLES)
    args = parser.parse_args()

    path = TABLES["entries" if args.command == "ask" else args.table]
    try:
        commit, table = read_table(path)
        source = f"{SOURCE}/{commit}/{path}"
        if args.command == "show":
            print(source, file=sys.stderr)
            print(table, end="")
            return 0

        query = normalize(args.name)
        try:
            matches = list(matching_entries(table, query))
        except RuntimeError as error:
            raise RuntimeError(f"{error}\n{source}") from None
        pending = list(matching_pending_notes(table, query))
        if not matches:
            if pending:
                for number, detail in pending:
                    print(f"待核  {detail}\n{source}#L{number}", file=sys.stderr)
            else:
                print(f"入口表未登记该名称。\n{source}", file=sys.stderr)
            return 1
        if len(matches) > 1:
            print("该名称对应多条入口。请用其中更具体的登记名查询。", file=sys.stderr)
            for number, _, entry in matches:
                print(f"{entry['说法']}  {source}#L{number}", file=sys.stderr)
            return 1

        number, heading, entry = matches[0]
        print(f"状态  {heading}")
        for key, value in entry.items():
            if key != "说法" and value:
                print(f"{key}  {value}")
        print(f"来源  {source}#L{number}")
        for number, detail in pending:
            print(f"待核  {detail}\n{source}#L{number}")
        return 0
    except FileNotFoundError:
        print("当前环境找不到 Git，无法查询入口表。", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("读取入口表超时。", file=sys.stderr)
    except subprocess.CalledProcessError as error:
        if "Permission denied (publickey)" in (error.stderr or ""):
            print("GitHub SSH 认证失败。请检查私有仓库的读取授权。", file=sys.stderr)
        else:
            print("读取 GitHub 私有仓库失败。请检查网络和仓库读取授权。", file=sys.stderr)
    except UnicodeError:
        print("入口表无法按 UTF-8 读取。", file=sys.stderr)
    except RuntimeError as error:
        print(error, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
