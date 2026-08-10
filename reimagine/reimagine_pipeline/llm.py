import base64
import json
import logging
import mimetypes
import subprocess
import urllib.request

logger = logging.getLogger(__name__)


class ClaudeCodeLLM:
    def __init__(self, model="opus", timeout=300, cli="claude", add_dir=None):
        self.model = model
        self.timeout = timeout
        self.cli = cli
        self.add_dir = add_dir
        self.log_reasoning = False

    def describe(self):
        return f"Claude Code CLI ({self.model}, multimodal via Read)"

    def chat(self, system_prompt, user_prompt, image_path, correction=None):
        user_prompt = (
            f"{user_prompt}\n\nUse the Read tool to inspect this image:\n"
            f"{image_path.resolve()}")
        if correction:
            user_prompt += f"\n\n{correction}"
        command = [
            self.cli, "-p", "--output-format", "json", "--model", self.model,
            "--system-prompt", system_prompt, "--allowedTools", "Read",
        ]
        if self.add_dir:
            command += ["--add-dir", str(self.add_dir)]
        process = subprocess.run(
            command, input=user_prompt, capture_output=True, text=True,
            timeout=self.timeout)
        if process.returncode != 0:
            raise RuntimeError(
                f"claude exited {process.returncode}: {process.stderr.strip()[:200]}")
        try:
            envelope = json.loads(process.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"claude returned non-JSON: {process.stdout.strip()[:200]!r}") from error
        if envelope.get("is_error") or envelope.get("subtype") != "success":
            raise RuntimeError(
                f"claude error envelope: subtype={envelope.get('subtype')}")
        return (envelope.get("result") or "").strip()


class OpenAILLM:
    def __init__(self, base_url, model=None, api_key=None, timeout=300):
        if "://" not in base_url:
            base_url = "http://" + base_url
        base_url = base_url.rstrip("/")
        self.base_url = base_url[:-3] if base_url.endswith("/v1") else base_url
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
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
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read())
        models = body.get("data") or body.get("models") or []
        if not models:
            raise RuntimeError(f"no models listed at {self.base_url}/v1/models")
        self.model = models[0].get("id") or models[0].get("model")
        return self.model

    def describe(self):
        return f"OpenAI-compatible server {self.base_url} (model={self.model})"

    def chat(self, system_prompt, user_prompt, image_path, correction=None):
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
            "max_tokens": 8192,
            "cache_prompt": True,
        }
        request = urllib.request.Request(
            self.base_url + "/v1/chat/completions",
            data=json.dumps(payload).encode(), headers=self._headers())
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read())
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
