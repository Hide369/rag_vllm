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


def _run_gpu_check(monkeypatch, stdout):
    class Completed:
        returncode = 0
        stderr = ""

    Completed.stdout = stdout
    fake_subprocess = types.ModuleType("subprocess")
    fake_subprocess.run = lambda *args, **kwargs: Completed()
    monkeypatch.setitem(sys.modules, "subprocess", fake_subprocess)
    exec(compile(_code_cell("# 1."), "gpu-check-cell", "exec"), {})


def test_gpu_check_rejects_gpu_smaller_than_l4(monkeypatch):
    with pytest.raises(RuntimeError, match="Tesla T4"):
        _run_gpu_check(monkeypatch, "Tesla T4, 15360 MiB\n")


@pytest.mark.parametrize(
    "stdout",
    [
        "NVIDIA L4, 23034 MiB\n",
        # L4 が取れないときの代わり。セル0の割合はL4基準なので、大きいGPUでは余るだけ
        "NVIDIA A100-SXM4-40GB, 40960 MiB\n",
        "NVIDIA H100 80GB HBM3, 81559 MiB\n",
    ],
)
def test_gpu_check_accepts_l4_or_larger(monkeypatch, capsys, stdout):
    _run_gpu_check(monkeypatch, stdout)
    assert stdout.split(",")[0] in capsys.readouterr().out


def test_gpu_check_rejects_unreadable_memory(monkeypatch):
    with pytest.raises(RuntimeError, match="メモリ"):
        _run_gpu_check(monkeypatch, "NVIDIA L4, [N/A]\n")


def _commands(calls):
    """起動した vLLM のコマンドを、--served-model-name をキーにして返す。"""
    commands = {}
    for args, _ in calls["popen"]:
        command = args[0]
        commands[command[command.index("--served-model-name") + 1]] = command
    return commands


def _option(command, name):
    return command[command.index(name) + 1]


def test_chat_embed_profile_starts_gpt_oss_and_bge_m3_on_separate_ports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = _install_process_fakes(monkeypatch, process=FakeProcess())

    namespace = _settings()
    assert namespace["VLLM_PROFILE"] == "chat+embed"
    exec(compile(_code_cell("# 3."), "vllm-start-cell", "exec"), namespace)

    commands = _commands(calls)
    assert set(commands) == {"gpt-oss-20b", "bge-m3"}

    chat = commands["gpt-oss-20b"]
    assert chat[:3] == ["/content/vllm-env/bin/vllm", "serve", "openai/gpt-oss-20b"]
    assert _option(chat, "--port") == "8001"
    assert _option(chat, "--host") == "127.0.0.1"
    # gpt-oss の道具呼び出し（Claude Code に必要）は公式手順の指定による
    assert "--enable-auto-tool-choice" in chat
    assert _option(chat, "--tool-call-parser") == "openai"

    embed = commands["bge-m3"]
    assert embed[:3] == ["/content/vllm-env/bin/vllm", "serve", "BAAI/bge-m3"]
    assert _option(embed, "--port") == "8002"
    assert _option(embed, "--runner") == "pooling"
    assert _option(embed, "--pooler-config.task") == "embed"
    assert json.loads(_option(embed, "--hf-overrides")) == {"architectures": ["BgeM3EmbeddingModel"]}

    assert set(namespace["vllm_procs"]) == {"chat", "embed"}


def test_chat_and_embed_memory_fractions_fit_on_one_gpu(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = _install_process_fakes(monkeypatch, process=FakeProcess())

    exec(compile(_code_cell("# 3."), "vllm-start-cell", "exec"), _settings())

    fractions = [
        float(_option(args[0], "--gpu-memory-utilization")) for args, _ in calls["popen"]
    ]
    assert len(fractions) == 2
    # 2つのプロセスのCUDAコンテキスト分の余裕を残す
    assert sum(fractions) <= 0.90


def test_vlm_profile_starts_only_qwen3_vl_on_8003(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = _install_process_fakes(monkeypatch, process=FakeProcess())

    namespace = _settings()
    namespace["VLLM_PROFILE"] = "vlm"
    exec(compile(_code_cell("# 3."), "vllm-start-cell", "exec"), namespace)

    commands = _commands(calls)
    assert set(commands) == {"qwen3-vl-8b"}
    vlm = commands["qwen3-vl-8b"]
    assert vlm[:3] == ["/content/vllm-env/bin/vllm", "serve", "Qwen/Qwen3-VL-8B-Instruct"]
    assert _option(vlm, "--port") == "8003"
    assert float(_option(vlm, "--gpu-memory-utilization")) <= 0.95


def test_vlm_embed_profile_starts_vlm_first_then_bge_m3_within_one_gpu(tmp_path, monkeypatch):
    # 取り込み（画像説明→埋め込み）は VLM と埋め込みを同時に要る
    monkeypatch.chdir(tmp_path)
    calls = _install_process_fakes(monkeypatch, process=FakeProcess())

    namespace = _settings()
    namespace["VLLM_PROFILE"] = "vlm+embed"
    exec(compile(_code_cell("# 3."), "vllm-start-cell", "exec"), namespace)

    served = [
        args[0][args[0].index("--served-model-name") + 1] for args, _ in calls["popen"]
    ]
    # 大きいほうを先に確保する。後から起動する側は空き容量を確かめて起動する
    assert served == ["qwen3-vl-8b", "bge-m3"]

    commands = _commands(calls)
    vlm = commands["qwen3-vl-8b"]
    assert _option(vlm, "--port") == "8003"
    assert _option(commands["bge-m3"], "--port") == "8002"
    fractions = [float(_option(command, "--gpu-memory-utilization")) for command in commands.values()]
    assert sum(fractions) <= 0.90
    # 埋め込みと同居するぶん文脈を縮めるので、大きな画像の視覚トークンに上限を付ける
    assert int(_option(vlm, "--max-model-len")) == 8192
    max_pixels = json.loads(_option(vlm, "--mm-processor-kwargs"))["max_pixels"]
    assert max_pixels // (32 * 32) < 8192
    assert json.loads(_option(vlm, "--limit-mm-per-prompt")) == {"video": 0}

    assert set(namespace["vllm_procs"]) == {"vlm", "embed"}


def test_unknown_profile_is_rejected():
    namespace = _settings()
    namespace["VLLM_PROFILE"] = "chat"

    with pytest.raises(ValueError, match="VLLM_PROFILE"):
        namespace["active_services"]()


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


def test_vllm_crash_shows_root_cause_buried_above_long_traceback(tmp_path, monkeypatch, capsys):
    # V1エンジンでは子プロセスの原因の後に、APIサーバー側の長いトレースバックが続く
    log_text = (
        "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB\n"
        + "  File \"/content/vllm-env/lib/python3.12/site-packages/vllm/x.py\", line 1\n" * 100
        + "RuntimeError: Engine core initialization failed. See root cause above.\n"
    )
    _install_process_fakes(
        monkeypatch, process=FakeProcess(returncode=1), models_status=None, log_text=log_text
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError):
        exec(compile(_code_cell("# 3."), "vllm-start-cell", "exec"), _settings())

    assert "CUDA out of memory" in capsys.readouterr().out


def test_vllm_runs_inside_its_venv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin")
    calls = _install_process_fakes(monkeypatch, process=FakeProcess())

    exec(compile(_code_cell("# 3."), "vllm-start-cell", "exec"), _settings())

    _, kwargs = calls["popen"][0]
    assert kwargs["env"]["VIRTUAL_ENV"] == "/content/vllm-env"
    assert kwargs["env"]["PATH"].startswith("/content/vllm-env/bin:")


def test_install_seeds_pip_and_downloads_kernels_inside_venv(monkeypatch):
    runs = []
    fake_subprocess = types.ModuleType("subprocess")
    fake_subprocess.run = lambda args, **kwargs: runs.append((args, kwargs))
    monkeypatch.setitem(sys.modules, "subprocess", fake_subprocess)

    exec(compile(_code_cell("# 2."), "install-cell", "exec"), _settings())

    venv_args = next(args for args, _ in runs if args[:2] == ["uv", "venv"])
    assert "--seed" in venv_args
    _, kernel_kwargs = next(
        (args, kwargs) for args, kwargs in runs if "download-kernels" in args
    )
    assert kernel_kwargs["env"]["VIRTUAL_ENV"] == "/content/vllm-env"
    assert kernel_kwargs["check"] is True


def test_key_cell_does_not_tell_user_to_put_key_in_env_file(monkeypatch, capsys):
    monkeypatch.delenv("PROXY_API_KEY", raising=False)
    exec(compile(_code_cell("# 4."), "proxy-key-cell", "exec"), _settings())
    assert ".env" not in capsys.readouterr().out


class SecretNotFoundError(Exception):
    """google.colab.userdata.SecretNotFoundError の代わり。"""


class NotebookAccessError(Exception):
    """google.colab.userdata.NotebookAccessError の代わり（シークレットへのアクセス未許可）。"""


class TimeoutException(Exception):
    """google.colab.userdata.TimeoutException の代わり（Colab UI 以外、または許可の確認に未応答）。"""


def _run_ngrok_cell(monkeypatch, *, secret, typed="typed-token", calls=None):
    """ngrok セルを偽の pyngrok / getpass / google.colab.userdata で実行し、呼び出し記録を返す。

    secret が例外なら userdata.get がそれを送出し、文字列ならその値を返す。
    セルが例外で止まる場合に備え、記録先の calls を外から渡せる。
    """
    calls = {"events": [], "prompts": []} if calls is None else calls
    calls.setdefault("events", [])
    calls.setdefault("prompts", [])
    config = types.SimpleNamespace(auth_token=None)

    pyngrok = types.ModuleType("pyngrok")
    conf = types.ModuleType("pyngrok.conf")
    conf.get_default = lambda: config
    ngrok = types.ModuleType("pyngrok.ngrok")
    ngrok.kill = lambda: calls["events"].append("kill")

    def connect(port, proto):
        calls["events"].append(("connect", port, proto, config.auth_token))
        return types.SimpleNamespace(public_url="https://example.ngrok-free.app")

    ngrok.connect = connect
    pyngrok.conf, pyngrok.ngrok = conf, ngrok

    getpass = types.ModuleType("getpass")

    def fake_getpass(prompt):
        calls["prompts"].append(prompt)
        return typed

    getpass.getpass = fake_getpass

    userdata = types.ModuleType("google.colab.userdata")
    userdata.SecretNotFoundError = SecretNotFoundError
    userdata.NotebookAccessError = NotebookAccessError
    userdata.TimeoutException = TimeoutException

    def get(name):
        assert name == "NGROK_AUTHTOKEN"
        if isinstance(secret, Exception):
            raise secret
        return secret

    userdata.get = get
    google = types.ModuleType("google")
    colab = types.ModuleType("google.colab")
    colab.userdata = userdata
    google.colab = colab

    for name, module in {
        "pyngrok": pyngrok,
        "pyngrok.conf": conf,
        "pyngrok.ngrok": ngrok,
        "getpass": getpass,
        "google": google,
        "google.colab": colab,
        "google.colab.userdata": userdata,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    settings = _settings()
    exec(compile(_code_cell("# 7."), "ngrok-cell", "exec"), settings)
    calls["port"] = settings["PROXY_PORT"]
    return calls


def test_ngrok_cell_does_not_tell_user_to_put_url_in_env_file(monkeypatch, capsys):
    _run_ngrok_cell(monkeypatch, secret="secret-token")

    out = capsys.readouterr().out
    assert "https://example.ngrok-free.app" in out
    assert ".env" not in out
    assert "OLLAMA_HOST" not in out


def test_ngrok_cell_uses_colab_secret_without_prompting(monkeypatch):
    calls = _run_ngrok_cell(monkeypatch, secret="  secret-token\n")

    assert calls["prompts"] == []
    # 再実行で前のトンネルが残らないよう、張る前に閉じる
    assert calls["events"] == ["kill", ("connect", calls["port"], "http", "secret-token")]


@pytest.mark.parametrize(
    "secret",
    [SecretNotFoundError("missing"), NotebookAccessError("denied"), TimeoutException("timed out"), ""],
)
def test_ngrok_cell_falls_back_to_prompt_when_secret_unavailable(monkeypatch, secret):
    calls = _run_ngrok_cell(monkeypatch, secret=secret, typed=" typed-token ")

    assert len(calls["prompts"]) == 1
    assert calls["events"][-1] == ("connect", calls["port"], "http", "typed-token")


def test_ngrok_cell_does_not_hide_unexpected_secret_errors(monkeypatch):
    with pytest.raises(RuntimeError, match="boom"):
        _run_ngrok_cell(monkeypatch, secret=RuntimeError("boom"))


def test_ngrok_cell_rejects_empty_token_before_connecting(monkeypatch):
    calls = {}
    with pytest.raises(ValueError, match="authtoken"):
        _run_ngrok_cell(monkeypatch, secret="", typed="   ", calls=calls)
    assert not any(event[0] == "connect" for event in calls["events"] if isinstance(event, tuple))


def test_vllm_restart_terminates_previous_process(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_process_fakes(monkeypatch, process=FakeProcess())
    previous = FakeProcess()

    namespace = _settings()
    namespace["vllm_procs"] = {"chat": previous}
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


def _bearer_request(key, method="POST"):
    class Request:
        query_params = {}
        headers = {"authorization": f"Bearer {key}", "content-type": "application/json"}

        async def body(self):
            return b"{}"

    Request.method = method
    return Request()


def _record_upstream_hosts(module):
    """どのルートのクライアントが送ったかを、base_url で記録する。"""
    sent_to = []
    for client in module._clients.values():
        original = client.send

        async def send(request, stream=False, _client=client, _original=original):
            sent_to.append(_client.kwargs["base_url"])
            return await _original(request, stream=stream)

        client.send = send
    return sent_to


def test_proxy_reads_key_at_runtime_and_routes_chat_to_the_chat_vllm(tmp_path, monkeypatch):
    secret = "runtime-only-secret"
    import secrets

    monkeypatch.delenv("PROXY_API_KEY", raising=False)
    monkeypatch.setattr(secrets, "token_urlsafe", lambda length: secret)
    namespace = _settings()
    exec(compile(_code_cell("# 4."), "proxy-key-cell", "exec"), namespace)
    assert os.environ["PROXY_API_KEY"] == secret
    assert namespace["PROXY_API_KEY"] == secret

    upstream = _Upstream([b'data: {"choices":[]}\n\n', b"data: [DONE]\n\n"])
    module, proxy_source = _load_proxy(tmp_path, monkeypatch, [upstream])
    assert secret not in proxy_source
    assert {p: c.kwargs["base_url"] for p, c in module._clients.items()} == {
        "chat": "http://127.0.0.1:8001",
        "embed": "http://127.0.0.1:8002",
    }
    sent_to = _record_upstream_hosts(module)

    async def exercise():
        response = await module.proxy("chat/v1/chat/completions", _bearer_request(secret))
        return b"".join([chunk async for chunk in response.body_iterator])

    body = asyncio.run(exercise())
    assert body == b'data: {"choices":[]}\n\ndata: [DONE]\n\n'
    assert sent_to == ["http://127.0.0.1:8001"]
    # 先頭の /chat は外して、vLLM には /v1/... として渡す
    assert upstream.request.path == "/v1/chat/completions"
    forwarded = {k.lower() for k, _ in upstream.request.headers}
    assert "authorization" not in forwarded
    assert "x-api-key" not in forwarded
    assert upstream.closed is True


def test_proxy_routes_embed_to_the_embedding_vllm(tmp_path, monkeypatch):
    monkeypatch.setenv("PROXY_API_KEY", "test-runtime-key")
    upstream = _Upstream([b"{}"])
    module, _ = _load_proxy(tmp_path, monkeypatch, [upstream])
    sent_to = _record_upstream_hosts(module)

    async def exercise():
        response = await module.proxy("embed/v1/embeddings", _bearer_request("test-runtime-key"))
        return response.status_code

    assert asyncio.run(exercise()) == 200
    assert sent_to == ["http://127.0.0.1:8002"]
    assert upstream.request.path == "/v1/embeddings"


def test_vlm_profile_proxy_routes_only_vlm(tmp_path, monkeypatch):
    monkeypatch.setenv("PROXY_API_KEY", "test-runtime-key")
    monkeypatch.chdir(tmp_path)
    namespace = _settings()
    namespace["VLLM_PROFILE"] = "vlm"
    exec(compile(_code_cell("# 5."), "proxy-generator-cell", "exec"), namespace)
    install_proxy_fakes(monkeypatch, [])
    module = types.ModuleType("vllm_proxy")
    exec(
        compile((tmp_path / "vllm_proxy.py").read_text(encoding="utf-8"), "vllm_proxy.py", "exec"),
        module.__dict__,
    )

    assert {p: c.kwargs["base_url"] for p, c in module._clients.items()} == {
        "vlm": "http://127.0.0.1:8003"
    }


def test_vlm_embed_profile_proxy_routes_vlm_and_embed(tmp_path, monkeypatch):
    monkeypatch.setenv("PROXY_API_KEY", "test-runtime-key")
    monkeypatch.chdir(tmp_path)
    namespace = _settings()
    namespace["VLLM_PROFILE"] = "vlm+embed"
    exec(compile(_code_cell("# 5."), "proxy-generator-cell", "exec"), namespace)
    install_proxy_fakes(monkeypatch, [])
    module = types.ModuleType("vllm_proxy")
    exec(
        compile((tmp_path / "vllm_proxy.py").read_text(encoding="utf-8"), "vllm_proxy.py", "exec"),
        module.__dict__,
    )

    assert {p: c.kwargs["base_url"] for p, c in module._clients.items()} == {
        "vlm": "http://127.0.0.1:8003",
        "embed": "http://127.0.0.1:8002",
    }


def test_proxy_rejects_missing_wrong_and_x_api_key_credentials_without_forwarding(
    tmp_path, monkeypatch
):
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
        return [
            await module.proxy("chat/v1/chat/completions", Request(headers))
            for headers in (
                {},
                {"authorization": "Bearer incorrect-key"},
                {"authorization": secret},
                # 旧方式の X-API-Key は、キーが合っていても受け付けない
                {"x-api-key": secret},
            )
        ]

    responses = asyncio.run(exercise())
    assert [r.status_code for r in responses] == [401, 401, 401, 401]
    assert upstreams == []
    assert secret not in json.dumps([r.body for r in responses])


def test_proxy_returns_404_for_unknown_prefix_without_forwarding(tmp_path, monkeypatch):
    monkeypatch.setenv("PROXY_API_KEY", "test-runtime-key")
    upstreams = []
    module, _ = _load_proxy(tmp_path, monkeypatch, upstreams)

    async def exercise():
        return (
            await module.proxy("vlm/v1/models", _bearer_request("test-runtime-key")),
            await module.proxy("v1/models", _bearer_request("test-runtime-key")),
        )

    unknown, unprefixed = asyncio.run(exercise())
    assert unknown.status_code == unprefixed.status_code == 404
    assert upstreams == []


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
            await module.proxy("chat/v1/chat/completions", _bearer_request("test-runtime-key")),
            await module.proxy("embed/v1/models", _bearer_request("test-runtime-key")),
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
    assert url == "http://127.0.0.1:8000/chat/v1/chat/completions"
    assert kwargs["headers"] == {"Authorization": "Bearer test-runtime-key"}
    assert kwargs["json"]["model"] == "gpt-oss-20b"
    assert kwargs["json"]["stream"] is True
    assert kwargs["json"]["stream_options"] == {"include_usage": True}
    assert kwargs["stream"] is True
    assert result["content"] == "東京です。"
    assert result["reasoning_chars"] == len("首都を答える。")
    assert result["completion_tokens"] == 40
    assert result["first_token_seconds"] > 0
    assert result["total_seconds"] >= result["first_token_seconds"]
    # Ollamaの eval rate と比べられるよう、初トークンまでの待ちを除いた生成速度にする
    decode_seconds = result["total_seconds"] - result["first_token_seconds"]
    assert result["decode_tokens_per_second"] == pytest.approx(40 / decode_seconds)
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


def _run_env_cell(public_url, profile="chat+embed"):
    namespace = _settings()
    namespace["VLLM_PROFILE"] = profile
    namespace["PROXY_API_KEY"] = "test-runtime-key"
    namespace["public_url"] = public_url
    exec(compile(_code_cell("# 9."), "env-cell", "exec"), namespace)


def test_env_cell_prints_vllm_settings_for_chat_and_embed(capsys):
    _run_env_cell("https://example.ngrok-free.app/")

    lines = capsys.readouterr().out.splitlines()
    assert "VLLM_CHAT_URL=https://example.ngrok-free.app/chat" in lines
    assert "VLLM_CHAT_MODEL=gpt-oss-20b" in lines
    assert "VLLM_EMBED_URL=https://example.ngrok-free.app/embed" in lines
    assert "VLLM_EMBED_MODEL=bge-m3" in lines
    assert "VLLM_API_KEY=test-runtime-key" in lines
    assert not any(line.startswith("VLLM_VLM_") for line in lines)


def test_env_cell_prints_only_vlm_settings_for_vlm_profile(capsys):
    _run_env_cell("https://example.ngrok-free.app", profile="vlm")

    lines = capsys.readouterr().out.splitlines()
    assert "VLLM_VLM_URL=https://example.ngrok-free.app/vlm" in lines
    assert "VLLM_VLM_MODEL=qwen3-vl-8b" in lines
    assert not any(line.startswith(("VLLM_CHAT_", "VLLM_EMBED_")) for line in lines)


def test_env_cell_prints_vlm_and_embed_settings_for_vlm_embed_profile(capsys):
    _run_env_cell("https://example.ngrok-free.app", profile="vlm+embed")

    lines = capsys.readouterr().out.splitlines()
    assert "VLLM_VLM_URL=https://example.ngrok-free.app/vlm" in lines
    assert "VLLM_VLM_MODEL=qwen3-vl-8b" in lines
    assert "VLLM_EMBED_URL=https://example.ngrok-free.app/embed" in lines
    assert "VLLM_EMBED_MODEL=bge-m3" in lines
    assert not any(line.startswith("VLLM_CHAT_") for line in lines)


def test_env_cell_does_not_write_any_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _run_env_cell("https://example.ngrok-free.app")

    assert list(tmp_path.iterdir()) == []
