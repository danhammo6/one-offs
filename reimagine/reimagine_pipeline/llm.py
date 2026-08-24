import base64
import json
import logging
import mimetypes
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)
_INTERRUPT_POLL_S = 0.05


def _uses_windows_process_tree():
    if os.name == "nt":
        return True
    platform = sys.platform
    return platform == "msys" or platform.startswith("cygwin")


def _cli_popen_kwargs():
    kwargs = dict(
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    if os.name == "nt":
        kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _close_quietly(stream):
    if stream is None:
        return
    try:
        fp = getattr(stream, "fp", stream)
        raw = getattr(fp, "raw", fp)
        sock = getattr(raw, "_sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    except Exception:
        pass
    try:
        stream.close()
    except Exception:
        pass


def _taskkill_tree(pid):
    if not pid:
        return
    if os.name == "nt":
        command = ["taskkill.exe", "/F", "/T", "/PID", str(pid)]
        extra = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    else:
        # MSYS converts "/F" into a filesystem path; "//F" reaches taskkill as /F.
        command = ["taskkill.exe", "//F", "//T", "//PID", str(pid)]
        extra = {}
    try:
        subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **extra)
    except OSError:
        pass


def _kill_process_group(process):
    if process.poll() is not None:
        return
    if _uses_windows_process_tree():
        _taskkill_tree(process.pid)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (AttributeError, ProcessLookupError, PermissionError, OSError):
            pass
    try:
        process.kill()
    except OSError:
        pass


def _run_interruptible(func, on_interrupt=None, poll=_INTERRUPT_POLL_S):
    """Run *func* in a daemon thread so Ctrl-C is not stuck in a blocking call."""
    result = {}

    def worker():
        try:
            result["value"] = func()
        except BaseException as error:
            result["error"] = error

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            # time.sleep is more reliably interrupted by Ctrl-C under MSYS2
            # than Thread.join; join still returns as soon as work finishes.
            if _uses_windows_process_tree():
                time.sleep(poll)
            else:
                thread.join(poll)
    except BaseException:
        if on_interrupt is not None:
            try:
                on_interrupt()
            except Exception:
                pass
        raise
    error = result.get("error")
    if error is not None:
        raise error
    return result.get("value")


def _urlopen_read(request, timeout):
    holder = {"response": None}

    def fetch():
        with urllib.request.urlopen(request, timeout=timeout) as response:
            holder["response"] = response
            return response.read()

    return _run_interruptible(
        fetch, on_interrupt=lambda: _close_quietly(holder["response"]))


class ClaudeCodeLLM:
    def __init__(self, model="opus", timeout=300, cli="claude", add_dir=None):
        self.model = model
        self.timeout = timeout
        self.cli = cli
        self.add_dir = add_dir
        self.log_reasoning = False

    def describe(self):
        return f"Claude Code CLI ({self.model}, multimodal via Read)"

    def chat(
            self, system_prompt, user_prompt, image_path, correction=None,
            json_schema=None):
        user_prompt = (
            f"{user_prompt}\n\nUse the Read tool to inspect this image:\n"
            f"{image_path.resolve()}")
        if json_schema is not None:
            user_prompt += (
                "\n\nReturn JSON matching this schema:\n" +
                json.dumps(json_schema, separators=(",", ":")))
        if correction:
            user_prompt += f"\n\n{correction}"
        command = [
            self.cli, "-p", "--output-format", "json", "--model", self.model,
            "--system-prompt", system_prompt, "--allowedTools", "Read",
        ]
        if self.add_dir:
            command += ["--add-dir", str(self.add_dir)]
        process = subprocess.Popen(command, **_cli_popen_kwargs())
        try:
            stdout, stderr = _run_interruptible(
                lambda: process.communicate(
                    input=user_prompt, timeout=self.timeout),
                on_interrupt=lambda: _kill_process_group(process))
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            raise
        if process.returncode != 0:
            raise RuntimeError(
                f"claude exited {process.returncode}: {stderr.strip()[:200]}")
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"claude returned non-JSON: {stdout.strip()[:200]!r}") from error
        if envelope.get("is_error") or envelope.get("subtype") != "success":
            raise RuntimeError(
                f"claude error envelope: subtype={envelope.get('subtype')}")
        return (envelope.get("result") or "").strip()


class OpenAILLM:
    def __init__(
            self, base_url, model=None, api_key=None, timeout=300,
            max_tokens=16384, reasoning="on"):
        if "://" not in base_url:
            base_url = "http://" + base_url
        base_url = base_url.rstrip("/")
        self.base_url = base_url[:-3] if base_url.endswith("/v1") else base_url
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.reasoning = reasoning
        self.log_reasoning = False

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def resolve_model(self):
        if self.model:
            return self.model
        request = urllib.request.Request(
            self.base_url + "/v1/models", headers=self._headers())
        body = json.loads(_urlopen_read(request, timeout=30))
        models = body.get("data") or body.get("models") or []
        if not models:
            raise RuntimeError(f"no models listed at {self.base_url}/v1/models")
        self.model = models[0].get("id") or models[0].get("model")
        return self.model

    def describe(self):
        return f"OpenAI-compatible server {self.base_url} (model={self.model})"

    def chat(
            self, system_prompt, user_prompt, image_path, correction=None,
            json_schema=None):
        self.resolve_model()
        raw = image_path.read_bytes()
        mime = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
        content = [
            {"type": "text", "text": user_prompt},
            {"type": "image_url", "image_url": {
                "url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"}},
        ]
        if correction:
            content.append({"type": "text", "text": correction})
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            "temperature": 0.7,
            "max_tokens": self.max_tokens,
            "cache_prompt": True,
            "reasoning": self.reasoning,
        }
        if json_schema is not None:
            payload["json_schema"] = json_schema
        request = urllib.request.Request(
            self.base_url + "/v1/chat/completions",
            data=json.dumps(payload).encode(), headers=self._headers())
        for attempt in range(2):
            try:
                body = json.loads(_urlopen_read(request, timeout=self.timeout))
                break
            except urllib.error.HTTPError as error:
                if error.code != 500 or attempt:
                    raise
                logger.warning(
                    "LLM request returned HTTP 500; retrying once in 1 second")
                time.sleep(1)
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError(f"no choices in response: {str(body)[:200]}")
        message = choices[0].get("message", {})
        usage = body.get("usage") or {}
        timings = body.get("timings") or {}
        logger.debug("LLM usage=%s timings=%s", usage, timings)
        reasoning = (message.get("reasoning_content") or "").strip()
        if self.log_reasoning and reasoning:
            logger.debug("LLM reasoning:\n%s", reasoning)
        return (message.get("content") or reasoning).strip()
