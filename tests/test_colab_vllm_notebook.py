"""colab/run_vllm_server.ipynb のコードセルを、偽の subprocess / requests で実行して検証する。

Colab の実機（L4・vLLM・ngrok）が無くても、起動引数・失敗時の止まり方・
認証プロキシの振る舞いを固定するため、tests/test_colab_agent_notebook.py と
同じくセルの本文を取り出して exec する。
"""
import asyncio
import json
import os
import sys
import types
from pathlib import Path

import pytest

from tests.notebook_proxy_fakes import install_proxy_fakes


NOTEBOOK = Path(__file__).parents[1] / "colab" / "run_vllm_server.ipynb"


def _notebook():
    return json.loads(NOTEBOOK.read_text(encoding="utf-8"))


def _code_cell(prefix):
    for cell in _notebook()["cells"]:
        source = "".join(cell.get("source", []))
        if cell["cell_type"] == "code" and source.startswith(prefix):
            return source
    raise AssertionError(f"code cell not found: {prefix}")


def _settings():
    """「# 0. 設定」セルを実行した名前空間。後続のセルはこの定数を使う。"""
    namespace = {}
    exec(compile(_code_cell("# 0."), "settings-cell", "exec"), namespace)
    return namespace


class FakeProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return self.returncode


def _install_process_fakes(monkeypatch, *, process, models_status=200, log_text=""):
    """vLLM起動セル用の偽 subprocess / requests / time を差し込み、呼び出し記録を返す。"""
    calls = {"popen": [], "sleep": []}

    fake_subprocess = types.ModuleType("subprocess")
    fake_subprocess.STDOUT = object()

    def popen(*args, **kwargs):
        calls["popen"].append((args, kwargs))
        # 実物の vLLM と同じく、渡されたログファイルへ書き込む
        kwargs["stdout"].write(log_text)
        kwargs["stdout"].flush()
        return process

    fake_subprocess.Popen = popen

    fake_requests = types.ModuleType("requests")
    fake_requests.RequestException = ConnectionError

    def get(url, **kwargs):
        if models_status is None:
            raise ConnectionError("connection refused")
        return types.SimpleNamespace(status_code=models_status)

    fake_requests.get = get

    fake_time = types.ModuleType("time")
    fake_time.sleep = lambda seconds: calls["sleep"].append(seconds)

    monkeypatch.setitem(sys.modules, "subprocess", fake_subprocess)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    monkeypatch.setitem(sys.modules, "time", fake_time)
    return calls


def test_gpu_check_rejects_non_l4_gpu(monkeypatch):
    class Completed:
        returncode = 0
        stdout = "Tesla T4, 15360 MiB\n"
        stderr = ""

    fake_subprocess = types.ModuleType("subprocess")
    fake_subprocess.run = lambda *args, **kwargs: Completed()
    monkeypatch.setitem(sys.modules, "subprocess", fake_subprocess)

    with pytest.raises(RuntimeError, match="Tesla T4"):
        exec(compile(_code_cell("# 1."), "gpu-check-cell", "exec"), {})


def test_gpu_check_accepts_l4(monkeypatch, capsys):
    class Completed:
        returncode = 0
        stdout = "NVIDIA L4, 23034 MiB\n"
        stderr = ""

    fake_subprocess = types.ModuleType("subprocess")
    fake_subprocess.run = lambda *args, **kwargs: Completed()
    monkeypatch.setitem(sys.modules, "subprocess", fake_subprocess)

    exec(compile(_code_cell("# 1."), "gpu-check-cell", "exec"), {})
    assert "NVIDIA L4" in capsys.readouterr().out


def test_vllm_starts_on_8001_with_spec_flags(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = _install_process_fakes(monkeypatch, process=FakeProcess())

    namespace = _settings()
    exec(compile(_code_cell("# 3."), "vllm-start-cell", "exec"), namespace)

    args, _ = calls["popen"][0]
    command = args[0]
    assert command == namespace["VLLM_COMMAND"]
    assert command[0] == "/content/vllm-env/bin/vllm"
    for expected in (
        "serve",
        "openai/gpt-oss-20b",
        "--port",
        "8001",
        "--max-model-len",
        "65536",
        "--gpu-memory-utilization",
        "0.90",
    ):
        assert expected in command
    assert command[command.index("--port") + 1] == "8001"
    assert namespace["vllm_proc"].poll() is None


def test_vllm_crash_fails_fast_with_log_tail(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    calls = _install_process_fakes(
        monkeypatch,
        process=FakeProcess(returncode=1),
        models_status=None,
        log_text="loading weights\ntorch.OutOfMemoryError: CUDA out of memory\n",
    )

    with pytest.raises(RuntimeError):
        exec(compile(_code_cell("# 3."), "vllm-start-cell", "exec"), _settings())

    # 落ちたプロセスを最大待ち時間（30分）待ち続けない
    assert sum(calls["sleep"]) < 60
    assert "CUDA out of memory" in capsys.readouterr().out


def test_vllm_restart_terminates_previous_process(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_process_fakes(monkeypatch, process=FakeProcess())
    previous = FakeProcess()

    namespace = _settings()
    namespace["vllm_proc"] = previous
    exec(compile(_code_cell("# 3."), "vllm-start-cell", "exec"), namespace)

    assert previous.terminated is True


def test_notebook_has_no_saved_execution_data_or_user_metadata():
    notebook = _notebook()

    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            assert cell.get("execution_count") is None
            assert cell.get("outputs") == []

        pending = [cell.get("metadata", {})]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                assert "executionInfo" not in value
                assert "user" not in value
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)


def _load_proxy(tmp_path, monkeypatch, upstreams):
    """セル5で vllm_proxy.py を書き出し、偽の FastAPI/httpx の上で読み込む。"""
    monkeypatch.chdir(tmp_path)
    exec(compile(_code_cell("# 5."), "proxy-generator-cell", "exec"), _settings())
    proxy_source = (tmp_path / "vllm_proxy.py").read_text(encoding="utf-8")
    install_proxy_fakes(monkeypatch, upstreams)
    module = types.ModuleType("vllm_proxy")
    exec(compile(proxy_source, "vllm_proxy.py", "exec"), module.__dict__)
    return module, proxy_source


class _Upstream:
    status_code = 200
    headers = {"content-type": "text/event-stream", "connection": "keep-alive"}

    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def aiter_raw(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


def _authorized_request(key):
    class Request:
        method = "POST"
        query_params = {}
        headers = {"x-api-key": key, "content-type": "application/json"}

        async def body(self):
            return b"{}"

    return Request()


def test_proxy_reads_key_at_runtime_and_streams_v1_routes(tmp_path, monkeypatch):
    secret = "runtime-only-secret"
    import secrets

    monkeypatch.delenv("PROXY_API_KEY", raising=False)
    monkeypatch.delenv("VLLM_UPSTREAM", raising=False)
    monkeypatch.setattr(secrets, "token_urlsafe", lambda length: secret)
    namespace = _settings()
    exec(compile(_code_cell("# 4."), "proxy-key-cell", "exec"), namespace)
    assert os.environ["PROXY_API_KEY"] == secret
    assert namespace["PROXY_API_KEY"] == secret

    upstream = _Upstream([b'data: {"choices":[]}\n\n', b"data: [DONE]\n\n"])
    module, proxy_source = _load_proxy(tmp_path, monkeypatch, [upstream])
    assert secret not in proxy_source
    assert module._client.kwargs["base_url"] == "http://127.0.0.1:8001"

    async def exercise():
        response = await module.proxy("v1/chat/completions", _authorized_request(secret))
        return b"".join([chunk async for chunk in response.body_iterator])

    body = asyncio.run(exercise())
    assert body == b'data: {"choices":[]}\n\ndata: [DONE]\n\n'
    assert upstream.request.path == "/v1/chat/completions"
    assert "x-api-key" not in {k.lower() for k, _ in upstream.request.headers}
    assert upstream.closed is True


def test_proxy_upstream_overridable_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PROXY_API_KEY", "test-runtime-key")
    monkeypatch.setenv("VLLM_UPSTREAM", "http://127.0.0.1:9999")
    module, _ = _load_proxy(tmp_path, monkeypatch, [])
    assert module._client.kwargs["base_url"] == "http://127.0.0.1:9999"


def test_proxy_rejects_missing_and_invalid_keys_without_forwarding(tmp_path, monkeypatch):
    secret = "test-runtime-key"
    monkeypatch.setenv("PROXY_API_KEY", secret)
    upstreams = []
    module, _ = _load_proxy(tmp_path, monkeypatch, upstreams)

    class Request:
        method = "POST"
        query_params = {}

        def __init__(self, headers):
            self.headers = headers

        async def body(self):
            raise AssertionError("unauthorized request body must not be read")

    async def exercise():
        return (
            await module.proxy("v1/chat/completions", Request({})),
            await module.proxy("v1/models", Request({"x-api-key": "incorrect-key"})),
        )

    missing, invalid = asyncio.run(exercise())
    assert missing.status_code == invalid.status_code == 401
    assert upstreams == []
    assert secret not in json.dumps([missing.body, invalid.body])


def test_proxy_maps_upstream_connection_and_timeout_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("PROXY_API_KEY", "test-runtime-key")
    upstreams = []
    module, _ = _load_proxy(tmp_path, monkeypatch, upstreams)
    upstreams.extend(
        [
            module.httpx.TimeoutException("read timed out"),
            module.httpx.RequestError("connection refused"),
        ]
    )

    async def exercise():
        return (
            await module.proxy("v1/chat/completions", _authorized_request("test-runtime-key")),
            await module.proxy("v1/models", _authorized_request("test-runtime-key")),
        )

    timeout, connection = asyncio.run(exercise())
    assert timeout.status_code == 504
    assert timeout.body == {"error": "vLLM upstream timed out"}
    assert connection.status_code == 502
    assert connection.body == {"error": "Unable to reach vLLM upstream"}


def _sse(payload):
    return "data: " + json.dumps(payload, ensure_ascii=False)


def _delta(**fields):
    return _sse({"choices": [{"index": 0, "delta": fields}]})


def _run_speed_cell(monkeypatch, lines, *, http_error=None):
    """偽の requests.post（SSE行を返す）と、呼ぶたびに0.5秒進む時計でセル8を実行する。"""
    calls = []

    class HTTPError(Exception):
        pass

    class Response:
        def raise_for_status(self):
            if http_error is not None:
                raise HTTPError(http_error)

        def iter_lines(self, decode_unicode=False):
            assert decode_unicode is True
            yield from lines

    fake_requests = types.ModuleType("requests")
    fake_requests.HTTPError = HTTPError

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    fake_requests.post = post

    clock = {"now": 100.0}

    def perf_counter():
        clock["now"] += 0.5
        return clock["now"]

    fake_time = types.ModuleType("time")
    fake_time.perf_counter = perf_counter

    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    monkeypatch.setitem(sys.modules, "time", fake_time)

    namespace = _settings()
    namespace["PROXY_API_KEY"] = "test-runtime-key"
    exec(compile(_code_cell("# 8."), "speed-check-cell", "exec"), namespace)
    return namespace, calls


def test_measure_chat_reports_reasoning_content_and_speed(monkeypatch, capsys):
    lines = [
        _delta(role="assistant"),
        "",
        _delta(reasoning="首都を"),
        _delta(reasoning="答える。"),
        _delta(content="東京"),
        _delta(content="です。"),
        _sse({"choices": [], "usage": {"completion_tokens": 40}}),
        "data: [DONE]",
    ]
    namespace, calls = _run_speed_cell(monkeypatch, lines)
    result = namespace["speed_result"]

    url, kwargs = calls[0]
    assert url == "http://127.0.0.1:8000/v1/chat/completions"
    assert kwargs["headers"] == {"X-API-Key": "test-runtime-key"}
    assert kwargs["json"]["model"] == "openai/gpt-oss-20b"
    assert kwargs["json"]["stream"] is True
    assert kwargs["json"]["stream_options"] == {"include_usage": True}
    assert kwargs["stream"] is True
    assert result["content"] == "東京です。"
    assert result["reasoning_chars"] == len("首都を答える。")
    assert result["completion_tokens"] == 40
    assert result["first_token_seconds"] > 0
    assert result["total_seconds"] >= result["first_token_seconds"]
    assert result["tokens_per_second"] == pytest.approx(40 / result["total_seconds"])
    assert "東京です。" in capsys.readouterr().out


def test_measure_chat_flags_reasoning_only_answer(monkeypatch, capsys):
    lines = [
        _delta(reasoning="考え中"),
        _sse({"choices": [], "usage": {"completion_tokens": 1024}}),
        "data: [DONE]",
    ]
    namespace, _ = _run_speed_cell(monkeypatch, lines)

    assert namespace["speed_result"]["content"] == ""
    assert "max_tokens" in capsys.readouterr().out


def test_measure_chat_raises_on_http_error(monkeypatch):
    with pytest.raises(Exception, match="401 Client Error"):
        _run_speed_cell(monkeypatch, [], http_error="401 Client Error: Unauthorized")
