"""colab/run_vllm_server.ipynb のコードセルを、偽の subprocess / requests で実行して検証する。

Colab の実機（L4・vLLM・ngrok）が無くても、起動引数・失敗時の止まり方・
認証プロキシの振る舞いを固定するため、tests/test_colab_agent_notebook.py と
同じくセルの本文を取り出して exec する。
"""
import json
import sys
import types
from pathlib import Path

import pytest


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
