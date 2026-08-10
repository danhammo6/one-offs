import json
import logging
import mimetypes
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ComfyArtifact:
    node_id: str
    filename: str
    subfolder: str
    storage_type: str


class ComfyClient:
    def __init__(self, server):
        if "://" in server:
            server = urllib.parse.urlparse(server).netloc or server.split("://", 1)[1]
        self.server = server.rstrip("/")
        self.client_id = str(uuid.uuid4())

    def ping(self):
        try:
            with urllib.request.urlopen(
                    f"http://{self.server}/system_stats", timeout=5) as response:
                return response.status == 200
        except Exception:
            return False

    def wait_until_up(self, poll=30.0):
        announced = False
        while not self.ping():
            if not announced:
                logger.warning("ComfyUI at %s unreachable; waiting", self.server)
                announced = True
            time.sleep(poll)

    def _history(self, prompt_id):
        with urllib.request.urlopen(
                f"http://{self.server}/history/{prompt_id}", timeout=30) as response:
            return json.loads(response.read())

    def run_workflow(self, workflow):
        import websocket
        prompt_id = str(uuid.uuid4())
        payload = json.dumps({
            "prompt": workflow, "client_id": self.client_id,
            "prompt_id": prompt_id,
        }).encode()
        socket = websocket.WebSocket()
        socket.connect(f"ws://{self.server}/ws?clientId={self.client_id}", timeout=30)
        try:
            request = urllib.request.Request(
                f"http://{self.server}/prompt", data=payload,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=30):
                pass
            while True:
                message = socket.recv()
                if not isinstance(message, str):
                    continue
                event = json.loads(message)
                data = event.get("data", {})
                if data.get("prompt_id") != prompt_id:
                    continue
                if event.get("type") == "execution_error":
                    raise RuntimeError(f"Comfy execution error: {data}")
                if event.get("type") == "executing" and data.get("node") is None:
                    break
        finally:
            socket.close()
        history = self._history(prompt_id).get(prompt_id, {})
        artifacts = []
        for node_id, output in history.get("outputs", {}).items():
            for raw in (output.get("images") or []) + (output.get("gifs") or []):
                artifacts.append(ComfyArtifact(
                    node_id, raw["filename"], raw.get("subfolder", ""),
                    raw.get("type", "output")))
        return artifacts

    def read_artifact(self, artifact, output_dir=None):
        if output_dir:
            path = output_dir / artifact.subfolder / artifact.filename
            if path.is_file():
                return path.read_bytes()
        query = urllib.parse.urlencode({
            "filename": artifact.filename, "subfolder": artifact.subfolder,
            "type": artifact.storage_type,
        })
        with urllib.request.urlopen(
                f"http://{self.server}/view?{query}", timeout=120) as response:
            return response.read()

    def upload_image(self, image_path, remote_name):
        boundary = f"----reimagine-{uuid.uuid4().hex}"
        mime = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
        subfolder, _, filename = remote_name.rpartition("/")
        fields = [("type", "input"), ("overwrite", "true")]
        if subfolder:
            fields.append(("subfolder", subfolder))
        body = bytearray()
        for name, value in fields:
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            body.extend(str(value).encode() + b"\r\n")
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'.encode())
        body.extend(f"Content-Type: {mime}\r\n\r\n".encode())
        body.extend(image_path.read_bytes())
        body.extend(f"\r\n--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            f"http://{self.server}/upload/image", data=bytes(body), headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read())
        returned_name = result.get("name") or filename
        returned_subfolder = result.get("subfolder") or subfolder
        return f"{returned_subfolder}/{returned_name}" if returned_subfolder else returned_name
