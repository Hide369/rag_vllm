"""Colabノートブックが書き出す認証プロキシを、FastAPI/httpx を入れずに実行するための偽モジュール。

Ollama版とvLLM版のノートブックのテストで共有する。
"""
import sys
import types


def install_proxy_fakes(monkeypatch, upstreams):
    httpx = types.ModuleType("httpx")

    class RequestError(Exception):
        pass

    class TimeoutException(RequestError):
        pass

    class Timeout:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class AsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def build_request(self, method, path, **kwargs):
            return types.SimpleNamespace(method=method, path=path, **kwargs)

        async def send(self, request, stream=False):
            assert stream is True
            result = upstreams.pop(0)
            if isinstance(result, BaseException):
                raise result
            result.request = request
            return result

    httpx.Timeout = Timeout
    httpx.RequestError = RequestError
    httpx.TimeoutException = TimeoutException
    httpx.AsyncClient = AsyncClient

    fastapi = types.ModuleType("fastapi")

    class FastAPI:
        def api_route(self, path, methods):
            def register(function):
                self.route = (path, methods, function)
                return function

            return register

    fastapi.FastAPI = FastAPI
    fastapi.Request = object

    responses = types.ModuleType("fastapi.responses")

    class JSONResponse:
        def __init__(self, body, status_code):
            self.body = body
            self.status_code = status_code

    class StreamingResponse:
        def __init__(self, body_iterator, status_code, headers):
            self.body_iterator = body_iterator
            self.status_code = status_code
            self.headers = headers

    responses.JSONResponse = JSONResponse
    responses.StreamingResponse = StreamingResponse

    monkeypatch.setitem(sys.modules, "httpx", httpx)
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    monkeypatch.setitem(sys.modules, "fastapi.responses", responses)
