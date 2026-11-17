"""Run an immutable automation snapshot in an isolated Docker container."""

import json
import queue
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from app.contracts import RunRequest, RunResult

_IMAGE = "python:3.12-alpine"
_TIMEOUT = 30.0
_MAX_FRAME = 65536
_MAX_LOG = 65536


class _APITimeout(TimeoutError):
    pass

# The protocol uses the original stdout; user prints and tracebacks go to stderr.
_BOOTSTRAP = r'''
import json, sys, traceback
protocol = sys.stdout
sys.stdout = sys.stderr

def send(value):
    data = json.dumps(value, separators=(",", ":"))
    if len(data.encode()) > 65536:
        raise ValueError("protocol frame too large")
    protocol.write(data + "\n")
    protocol.flush()

def api(operation, body):
    send({"type": "call", "operation": operation, "body": body})
    line = sys.stdin.buffer.readline(65537)
    if not line or len(line) > 65536:
        raise RuntimeError("invalid API response")
    response = json.loads(line)
    if "exception" in response:
        raise RuntimeError(response["exception"])
    return response["result"]

try:
    event = json.loads(sys.stdin.buffer.readline(65537))
    sys.path.insert(0, "/workspace")
    from automation import handle
    status = handle(event, api)
    if status not in ("waiting", "completed"):
        raise ValueError("handle must return waiting or completed")
    send({"type": "result", "status": status})
except BaseException:
    traceback.print_exc(file=sys.stderr)
    send({"type": "error", "error": traceback.format_exc()[-4096:]})
'''


class Runner:
    def __init__(self, snapshot, call_api):
        self.snapshot = snapshot
        self.call_api = call_api

    def execute(self, run: RunRequest) -> RunResult:
        name = "self-coding-" + uuid.uuid4().hex
        logs = bytearray()
        process = None
        log_thread = None
        stop_reader = threading.Event()
        deadline = time.monotonic() + _TIMEOUT
        try:
            with tempfile.TemporaryDirectory(prefix="self-coding-run-") as temp:
                root = Path(temp)
                workspace = root / "workspace"
                workspace.mkdir()
                self.snapshot(run.project_id, run.commit, workspace)
                bootstrap = root / "bootstrap.py"
                bootstrap.write_text(_BOOTSTRAP)
                command = [
                    "docker", "run", "--rm", "-i", "--name", name,
                    "--network", "none", "--read-only", "--memory", "256m",
                    "--cpus", "1", "--pids-limit", "64", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges", "--user", "65534:65534",
                    "--mount", f"type=bind,src={workspace},dst=/workspace,readonly",
                    "--mount", f"type=bind,src={bootstrap},dst=/bootstrap.py,readonly",
                    _IMAGE, "python", "-I", "-B", "/bootstrap.py",
                ]
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                frames = queue.Queue(maxsize=1)

                def read_frames():
                    while True:
                        line = process.stdout.readline(_MAX_FRAME + 1)
                        while not stop_reader.is_set():
                            try:
                                frames.put(line, timeout=0.1)
                                break
                            except queue.Full:
                                pass
                        if stop_reader.is_set():
                            return
                        if not line or len(line) > _MAX_FRAME:
                            return

                def read_logs():
                    while chunk := process.stderr.read(4096):
                        if len(logs) < _MAX_LOG:
                            logs.extend(chunk[:_MAX_LOG - len(logs)])

                threading.Thread(target=read_frames, daemon=True).start()
                log_thread = threading.Thread(target=read_logs, daemon=True)
                log_thread.start()

                def send(value):
                    data = json.dumps(value, allow_nan=False, separators=(",", ":")).encode() + b"\n"
                    if len(data) > _MAX_FRAME:
                        raise ValueError("protocol frame too large")
                    outcome = queue.Queue(maxsize=1)

                    def write():
                        try:
                            process.stdin.write(data)
                            process.stdin.flush()
                            outcome.put(None)
                        except Exception as exc:
                            outcome.put(exc)

                    threading.Thread(target=write, daemon=True).start()
                    try:
                        error = outcome.get(timeout=max(0, deadline - time.monotonic()))
                    except queue.Empty:
                        raise TimeoutError("container deadline exceeded") from None
                    if error is not None:
                        raise error

                send(asdict(run.event))
                result = None
                while result is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("container deadline exceeded")
                    try:
                        line = frames.get(timeout=remaining)
                    except queue.Empty:
                        raise TimeoutError("container deadline exceeded") from None
                    if not line or len(line) > _MAX_FRAME or not line.endswith(b"\n"):
                        raise ValueError("invalid container frame")
                    frame = json.loads(line)
                    if not isinstance(frame, dict):
                        raise ValueError("invalid container frame")
                    if frame.get("type") == "call":
                        operation, body = frame.get("operation"), frame.get("body")
                        if not isinstance(operation, str) or not isinstance(body, dict):
                            raise ValueError("invalid API call")
                        responses = queue.Queue(maxsize=1)

                        def invoke():
                            try:
                                responses.put({"result": self.call_api(run, operation, body)})
                            except Exception as exc:
                                responses.put({"exception": str(exc)[:4096]})

                        threading.Thread(target=invoke, daemon=True).start()
                        try:
                            response = responses.get(timeout=max(0, deadline - time.monotonic()))
                        except queue.Empty:
                            raise _APITimeout("API call exceeded container deadline; outcome unknown") from None
                        send(response)
                    elif frame.get("type") == "result" and frame.get("status") in ("waiting", "completed"):
                        result = RunResult(frame["status"])
                    elif frame.get("type") == "error" and isinstance(frame.get("error"), str):
                        result = RunResult("failed", error=frame["error"][:4096])
                    else:
                        raise ValueError("invalid container frame")
                process.stdin.close()
                try:
                    code = process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    raise TimeoutError("container deadline exceeded") from None
                log_thread.join(timeout=max(0, deadline - time.monotonic()))
                if code and result.status != "failed":
                    result = RunResult("failed", error=f"container exited with status {code}")
        except Exception as exc:
            result = RunResult("uncertain" if isinstance(exc, _APITimeout) else "failed", error=str(exc)[:4096], retryable=isinstance(exc, TimeoutError) and not isinstance(exc, _APITimeout))
        finally:
            stop_reader.set()
            if process is not None:
                if process.poll() is None:
                    process.kill()
                try:
                    subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                if log_thread is not None:
                    log_thread.join(timeout=1)
        return RunResult(result.status, [logs.decode("utf-8", "replace")] if logs else [], result.error, result.retryable)
