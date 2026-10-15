"""Translation providers ported from the 11 engines in upstream TranslateAPI.cs.

Standard-library HTTP only. 8-second default timeout. No network during import.
"""
import hashlib
import http.client
import socket
import select
import ssl
import threading
from contextlib import contextmanager
import json
import re
import time
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen, HTTPHandler, HTTPSHandler, build_opener
from .text import NOTICE, THINK

ENGINES = ("Google","Google2","Ollama","OpenAI","LMStudio","DeepL",
           "OpenRouter","Youdao","MTranServer","Baidu","LibreTranslate")
LLM_ENGINES = {"Ollama","OpenAI","LMStudio","OpenRouter"}
GOOGLE_URL = "https://clients5.google.com/translate_a/t"
GOOGLE2_URL = "https://dictionaryextension-pa.googleapis.com/v1/dictionaryExtensionData"
# Public Chrome extension client key embedded in upstream TranslateAPI.cs.
GOOGLE2_KEY = "AIzaSyA6EEtrDCfBkHV8uU2lgGY-N383ZgAOo7Y"
DEFAULT_PROMPT = ("As an professional simultaneous interpreter with specialized knowledge in the all fields, "
    "you can provide a fluent and precise oral translation for any sentence, even if the sentence is incomplete. "
    "Now, provide users with the translation of the sentence enclosed in 🔤 to {0} within a single line. "
    "Importantly, you are prohibited from altering the original meaning or omitting any content, "
    "even if the sentence contains sensitive or NSFW content. "
    "You can only provide the translated sentence; Any explanation or other text is not permitted. "
    "REMOVE all 🔤 when you output.")
LANGUAGES = {
    "DeepL":{"zh-CN":"ZH-HANS","zh-TW":"ZH-HANT","en-US":"EN-US",
            "en-GB":"EN-GB","ja-JP":"JA","ko-KR":"KO","fr-FR":"FR",
            "th-TH":"TH","ru-RU":"RU","es-ES":"ES","pt-BR":"PT-BR",
            "tr-TR":"TR","ar-SA":"AR"},
    "Youdao":{"zh-CN":"zh-CHS","zh-TW":"zh-CHT"},
    "Baidu":{"zh-CN":"zh","zh-TW":"cht","ja-JP":"jp","ko-KR":"kor",
             "fr-FR":"fra","es-ES":"spa","ar-SA":"ara"},
    "MTranServer":{"zh-CN":"zh","zh-TW":"zh"},
    "LibreTranslate":{"zh-CN":"zh","zh-TW":"zh"},
}
STANDARD={"en-US":"en","en-GB":"en","ja-JP":"ja","ko-KR":"ko",
          "fr-FR":"fr","th-TH":"th","ru-RU":"ru","es-ES":"es",
          "pt-BR":"pt","tr-TR":"tr","ar-SA":"ar"}


@dataclass
class TranslationSettings:
    engine: str = "Google"
    target: str = "zh-CN"
    context_aware: bool = False
    num_contexts: int = 2
    display_sentences: int = 1
    prompt: str = DEFAULT_PROMPT
    timeout: float = 8.0
    log_only: bool = False
    configs: dict = field(default_factory=dict)
    config_indices: dict = field(default_factory=dict)

    @classmethod
    def from_file(cls,path):
        from pathlib import Path
        file=Path(path)
        if not file.is_file():
            return cls()
        data=json.loads(file.read_text(encoding="utf-8-sig"))
        if not isinstance(data,dict):
            raise ValueError("translation settings must be an object")
        instance=cls(**{k:v for k,v in data.items() if k in cls.__dataclass_fields__})
        if instance.engine not in ENGINES:
            raise ValueError("unknown translation engine: "+str(instance.engine))
        if not isinstance(instance.configs,dict):
            raise ValueError("configs must be an object")
        if not isinstance(instance.config_indices, dict):
            raise ValueError("config_indices must be an object")
        return instance

    def config(self):
        configurations = self.configs.get(self.engine, {}) or {}
        if isinstance(configurations, list):
            index = int(self.config_indices.get(self.engine, 0))
            if not 0 <= index < len(configurations):
                raise ValueError("invalid configuration index for " + self.engine)
            return configurations[index]
        if not isinstance(configurations, dict):
            raise ValueError("configuration must be an object or a list")
        return configurations


def _config(config,key,default=""):
    # Accept both Python snake_case and original C# PascalCase keys.
    pascal="".join(s.title() for s in key.split("_"))
    if key == "keep_alive":
        return config.get("keep_alive",config.get("Keep_alive",config.get("KeepAlive",default)))
    return config.get(key,config.get(pascal,default))


def normalize_url(url):
    """TextUtil.NormalizeUrl: preserve http(s) prefix, collapse other slashes."""
    match=re.match(r"^https?://",url)
    prefix=match.group(0) if match else ""
    return prefix+re.sub(r"/{2,}","/",url[len(prefix):]).rstrip("/")



class TranslationCancelled(Exception):
    """An obsolete translation task cancelled its active HTTP transport."""


class CancellationToken:
    """The per-task cancellation source, mirroring .NET CTS ownership.

    Cancellation shuts down tracked TCP/TLS sockets, unblocking concurrent
    response reads rather than merely suppressing a finished translation.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._cancelled = False
        self._deadline = None
        self._sockets = set()

    @property
    def cancelled(self):
        with self._lock:
            return self._cancelled

    @property
    def deadline(self):
        with self._lock:
            return self._deadline

    def start_request(self, timeout):
        with self._lock:
            self._deadline = time.monotonic() + float(timeout)
        self.check()

    def check(self):
        with self._lock:
            cancelled = self._cancelled
            deadline = self._deadline
        if cancelled:
            raise TranslationCancelled("translation request cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("timed out")

    def register(self, sock):
        if sock is None:
            return
        # urllib/http.client closes its connection socket immediately after
        # response headers, while a buffered makefile continues reading the
        # body. Hold an OS-level duplicate that remains open for cancellation.
        duplicate = sock.dup()
        with self._lock:
            cancelled = self._cancelled
            if not cancelled:
                self._sockets.add(duplicate)
        if cancelled:
            try:
                duplicate.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            duplicate.close()
            raise TranslationCancelled("translation request cancelled")

    def finish_request(self):
        with self._lock:
            sockets = tuple(self._sockets)
            self._sockets.clear()
            self._deadline = None
        for sock in sockets:
            sock.close()

    def cancel(self):
        with self._lock:
            self._cancelled = True
            sockets = tuple(self._sockets)
            self._sockets.clear()
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass



class _PollingHTTPResponse(http.client.HTTPResponse):
    """Poll cancellability before each underlying socket recv.

    Windows can leave a blocking recv in progress after shutdown of a
    duplicated socket. Wrapping the unbuffered SocketIO readinto (before
    response headers) avoids that kernel-level cancellation race while
    preserving the stdlib's redirect, chunked decoding and buffering.
    """
    def __init__(self, sock, *, cancel_token, **kwargs):
        super().__init__(sock, **kwargs)
        raw = self.fp.raw
        receive = raw.readinto
        deadline = cancel_token.deadline or (
            time.monotonic() + (sock.gettimeout() or 8))

        def cancellable_readinto(buffer):
            while True:
                cancel_token.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out")
                transport = raw._sock
                if isinstance(transport, ssl.SSLSocket) and transport.pending():
                    readable = [transport]
                else:
                    readable, _, _ = select.select(
                        [transport], [], [], min(0.04, remaining))
                if readable:
                    cancel_token.check()
                    try:
                        n = receive(buffer)
                    except (ssl.SSLWantReadError, ssl.SSLWantWriteError,
                            BlockingIOError):
                        continue
                    if n is None:
                        continue
                    return n

        raw.readinto = cancellable_readinto


class _TrackedConnection:
    """Mixin registers TCP connection immediately after connect, before IO."""
    def __init__(self, *args, cancel_token, **kwargs):
        self._cancel_token = cancel_token
        super().__init__(*args, **kwargs)
        self.response_class = lambda sock, **kw: _PollingHTTPResponse(
            sock, cancel_token=cancel_token, **kw)
        original = self._create_connection

        def tracked_connect(*connect_args, **connect_kwargs):
            cancel_token.check()
            sock = original(*connect_args, **connect_kwargs)
            cancel_token.register(sock)
            return sock

        self._create_connection = tracked_connect

    def send(self, data):
        """Nonblocking cancellable send of headers and request bodies."""
        if self.sock is None:
            self.connect()
        if hasattr(data, "read"):
            for block in iter(lambda: data.read(self.blocksize), b""):
                self.send(block)
            return
        if isinstance(data, str):
            data = data.encode("iso-8859-1")
        remaining_bytes = memoryview(data)
        while remaining_bytes:
            self._cancel_token.check()
            deadline = self._cancel_token.deadline
            remaining_time = (deadline - time.monotonic()
                              if deadline is not None else self.timeout)
            if remaining_time <= 0:
                raise TimeoutError("timed out")
            try:
                written = self.sock.send(remaining_bytes)
                if written <= 0:
                    raise ConnectionError("translation HTTP socket closed")
                remaining_bytes = remaining_bytes[written:]
            except (ssl.SSLWantReadError, BlockingIOError):
                select.select([self.sock], [], [], min(.04, remaining_time))
            except ssl.SSLWantWriteError:
                select.select([], [self.sock], [], min(.04, remaining_time))


class _TrackedHTTPConnection(_TrackedConnection, http.client.HTTPConnection):
    def connect(self):
        self._cancel_token.check()
        super().connect()
        self._cancel_token.check()
        self.sock.setblocking(False)


class _TrackedHTTPSConnection(_TrackedConnection, http.client.HTTPSConnection):
    def connect(self):
        self._cancel_token.check()
        # Equivalent to HTTPSConnection.connect, but register the SSL socket
        # before TLS handshake to make a blocked handshake cancellable too.
        http.client.HTTPConnection.connect(self)
        host = self._tunnel_host or self.host
        try:
            ssl_sock = self._context.wrap_socket(
                self.sock, server_hostname=host, do_handshake_on_connect=False)
            self.sock = ssl_sock
            # SSLSocket cannot be dup()'ed. Its underlying TCP handle is
            # already tracked, and TLS negotiation itself is cooperatively
            # polled to honor cancellation during handshake.
            ssl_sock.setblocking(False)
            deadline = self._cancel_token.deadline or (
                time.monotonic() + self.timeout)
            try:
                while True:
                    self._cancel_token.check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("timed out")
                    try:
                        ssl_sock.do_handshake()
                        break
                    except ssl.SSLWantReadError:
                        select.select([ssl_sock], [], [], min(0.04, remaining))
                    except ssl.SSLWantWriteError:
                        select.select([], [ssl_sock], [], min(0.04, remaining))
            finally:
                # Keep TLS reads nonblocking: all SocketIO recv attempts run
                # through the cancellable select/readinto guard.
                ssl_sock.setblocking(False)
            self._cancel_token.check()
        except BaseException:
            self.close()
            raise


class _CancellableHTTPHandler(HTTPHandler):
    def __init__(self, token):
        super().__init__()
        self._token = token

    def http_open(self, req):
        return self.do_open(
            lambda host, **kw: _TrackedHTTPConnection(
                host, cancel_token=self._token, **kw), req)


class _CancellableHTTPSHandler(HTTPSHandler):
    def __init__(self, token):
        super().__init__()
        self._token = token

    def https_open(self, req):
        return self.do_open(
            lambda host, **kw: _TrackedHTTPSConnection(
                host, cancel_token=self._token, **kw), req)


def _request(url, *, data=None, headers=None, timeout=8.0, form=False, cancel_token=None):
    request_headers=dict(headers or {})
    payload=None
    if data is not None:
        if form:
            payload=urlencode(data).encode("utf-8")
            request_headers.setdefault("Content-Type","application/x-www-form-urlencoded")
        else:
            payload=json.dumps(data,ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type","application/json")
    request=Request(url,data=payload,headers=request_headers,
                    method="POST" if data is not None else "GET")
    if cancel_token is not None:
        cancel_token.start_request(timeout)
        opener = build_opener(_CancellableHTTPHandler(cancel_token),
                              _CancellableHTTPSHandler(cancel_token))
        open_request = opener.open
    else:
        open_request = urlopen
    try:
        with open_request(request, timeout=timeout) as result:
            payload = result.read()
            if cancel_token is not None:
                cancel_token.check()
            return json.loads(payload.decode("utf-8"))
    except HTTPError as error:
        if cancel_token is not None and cancel_token.cancelled:
            raise TranslationCancelled("translation request cancelled") from error
        raise ValueError(f"HTTP Error - {error.code}") from error
    except Exception as error:
        if cancel_token is not None and cancel_token.cancelled:
            raise TranslationCancelled("translation request cancelled") from error
        raise
    finally:
        if cancel_token is not None:
            cancel_token.finish_request()


def google_translate(text,target="zh-CN",timeout=8.0):
    url=GOOGLE_URL+"?"+urlencode({"client":"dict-chrome-ex","sl":"auto",
                                  "tl":target,"q":text})
    return _request(url,timeout=timeout)[0][0]


class TranslationAPI:
    def __init__(self,settings=None,requester=None):
        self.settings=settings or TranslationSettings()
        self.requester=requester or _request
        self._openai_fallback_index=0
        self._cancellation_local = threading.local()

    @contextmanager
    def cancellation(self,token):
        previous = getattr(self._cancellation_local,"token",None)
        self._cancellation_local.token = token
        try:
            if token is not None:
                token.check()
            yield
        finally:
            self._cancellation_local.token = previous

    def _r(self,url,**kwargs):
        kwargs.setdefault("timeout",self.settings.timeout)
        token = getattr(self._cancellation_local,"token",None)
        if token is not None:
            token.check()
            # Injected requesters from existing tests have the old signature.
            if self.requester is _request:
                kwargs["cancel_token"] = token
        return self.requester(url,**kwargs)

    def _lang(self,engine):
        target=self.settings.target
        mapping=LANGUAGES.get(engine,{})
        return mapping.get(target, STANDARD.get(target, target) if engine in
                           {"Youdao","Baidu","MTranServer","LibreTranslate"} else target)

    def _messages(self,text,contexts):
        messages=[{"role":"system","content":self.settings.prompt.format(self.settings.target)},
                  {"role":"user","content":f"🔤 {text} 🔤"}]
        if self.settings.context_aware:
            for entry in contexts:
                source=getattr(entry,"source","")
                translated=getattr(entry,"translated","")
                if "[ERROR]" in translated or "[WARNING]" in translated:
                    continue
                # Upstream inserts the context pair at index 1, newest first.
                messages[1:1]=[{"role":"user","content":f"🔤 {source} 🔤"},
                               {"role":"assistant","content":NOTICE.sub("",translated)}]
        return messages

    def translate(self,text,contexts=()):
        engine=self.settings.engine
        cfg=self.settings.config()
        lang=self._lang(engine)
        try:
            if engine=="Google":
                url=GOOGLE_URL+"?"+urlencode({"client":"dict-chrome-ex",
                                               "sl":"auto","tl":self.settings.target,"q":text})
                return self._r(url)[0][0]
            if engine=="Google2":
                url=GOOGLE2_URL+"?"+urlencode({"language":self.settings.target,
                    "key":_config(cfg,"api_key",GOOGLE2_KEY),"term":text,"strategy":"2"})
                reply = self._r(url, headers={"x-referer":
                    "chrome-extension://mgijmajocgfcbeboacabfgobmjgjcoja"})
                if "translateResponse" not in reply:
                    return "[ERROR] Translation Failed: Unexpected API response format"
                return reply["translateResponse"]["translateText"]
            if engine in LLM_ENGINES:
                return self._llm(engine,text,contexts,cfg)
            if engine=="DeepL":
                answer=self._r(normalize_url(_config(cfg,"api_url","https://api.deepl.com/v2/translate")),
                    data={"text":[text],"target_lang":lang},
                    headers={"Authorization":"DeepL-Auth-Key "+_config(cfg,"api_key")})
                return answer["translations"][0]["text"]
            if engine=="Youdao":
                salt=str(int(time.time()*1000)%1000)
                sign=hashlib.md5((_config(cfg,"app_key")+text+salt+
                                  _config(cfg,"app_secret")).encode("utf-8")).hexdigest()
                answer=self._r(_config(cfg,"api_url","https://openapi.youdao.com/api"),
                    data={"q":text,"from":"auto","to":lang,"appKey":_config(cfg,"app_key"),
                          "salt":salt,"sign":sign},form=True)
                if str(answer.get("errorCode","0"))!="0":
                    return "[ERROR] Translation Failed: Youdao Error - "+str(answer["errorCode"])
                return next(iter(answer.get("translation",[])),"[ERROR] Translation Failed: No content")
            if engine=="MTranServer":
                answer=self._r(normalize_url(_config(cfg,"api_url","http://localhost:8989/translate")),
                    data={"text":text,"to":lang,"from":_config(cfg,"source_language","en")},
                    headers={"Authorization":"Bearer "+_config(cfg,"api_key")})
                return answer["result"]
            if engine=="Baidu":
                salt=str(int(time.time()*1000)%1000)
                sign=hashlib.md5((_config(cfg,"app_id")+text+salt+
                                  _config(cfg,"app_secret")).encode("utf-8")).hexdigest()
                answer=self._r(_config(cfg,"api_url","https://fanyi-api.baidu.com/api/trans/vip/translate"),
                    data={"q":text,"from":"auto","to":lang,
                          "appid":_config(cfg,"app_id"),"salt":salt,"sign":sign},form=True)
                if answer.get("error_code") and str(answer["error_code"])!="0":
                    return "[ERROR] Translation Failed: Baidu Error - "+str(answer["error_code"])
                return next((e["dst"] for e in answer.get("trans_result",[])),
                            "[ERROR] Translation Failed: No content")
            if engine=="LibreTranslate":
                answer=self._r(normalize_url(_config(cfg,"api_url","http://localhost:5000/translate")),
                    data={"q":text,"target":lang,"source":"auto","format":"text",
                          "api_key":_config(cfg,"api_key")})
                return answer["translatedText"]
            raise ValueError("Unsupported engine "+str(engine))
        except TranslationCancelled:
            raise
        except Exception as exc:
            reason=str(getattr(exc,"reason",""))
            if isinstance(exc,TimeoutError) or (isinstance(exc,URLError) and "timed out" in reason.lower()):
                return ("[ERROR] Translation Failed: The request was canceled due to timeout (> 8 seconds), "
                        "please use a faster API or check network connection.")
            return "[ERROR] Translation Failed: "+str(exc)

    @staticmethod
    def _request_data(platform, model, messages, temperature, keep_alive=600):
        """Exact upstream RequestData.cs / LLMRequestDataFactory.cs fields."""
        base = {"model": model, "messages": messages,
                "temperature": float(temperature), "max_tokens": 128,
                "stream": False, "keep_alive": int(keep_alive)}
        if platform == "integrated":
            base.update(think=False, enable_thinking=False, reasoning_effort="low",
                        reasoning={"exclude": True, "enabled": False, "effort": "low"},
                        thinking={"type": "disabled"})
        elif platform == "Aliyun":
            base["enable_thinking"] = False
        elif platform == "Anthropic":
            base["thinking"] = {"type": "disabled"}
        elif platform == "Ollama":
            base["think"] = False
        elif platform == "OpenRouter":
            base["reasoning"] = {"exclude": True, "enabled": False}
        elif platform == "OpenAI":
            base["reasoning"] = {"effort": "low"}
        elif platform == "XAI":
            base["reasoning_effort"] = "low"
        return base

    def _llm(self,engine,text,contexts,cfg):
        model=_config(cfg,"model_name")
        temperature=float(_config(cfg,"temperature",1.0))
        messages=self._messages(text,contexts)
        if engine=="LMStudio":
            url=normalize_url(_config(cfg,"api_url","http://localhost:1234/api/v1")+"/chat")
            entry=f"🔤 {text} 🔤"
            if self.settings.context_aware:
                lines=[f"🔤 {e.source} 🔤 → {NOTICE.sub('',e.translated)}" for e in contexts
                       if "[ERROR]" not in e.translated and "[WARNING]" not in e.translated]
                if lines:
                    entry="\n".join(lines)+"\n"+entry
            data={"model":model,"system_prompt":self.settings.prompt.format(self.settings.target),
                  "input":entry,"temperature":temperature}
            reply=self._r(url,data=data)
            return next((THINK.sub("",item.get("content","")) for item in reply.get("output",[])
                         if item.get("type")=="message"),
                        "[ERROR] Translation Failed: Unexpected response format")
        if engine=="Ollama":
            url=normalize_url(_config(cfg,"api_url","http://localhost:11434")+"/api/chat")
            reply=self._r(url,data=self._request_data(
                "Ollama",model,messages,temperature,_config(cfg,"keep_alive",600)))
            return THINK.sub("",reply["message"]["content"])

        url=("https://openrouter.ai/api/v1/chat/completions" if engine=="OpenRouter"
             else normalize_url(_config(cfg,"api_url")))
        headers={"Authorization":"Bearer "+_config(cfg,"api_key")}
        if engine=="OpenRouter":
            data=self._request_data("OpenRouter",model,messages,temperature)
            reply=self._r(url,data=data,headers=headers)
        else:
            # Upstream attempts all eight request schemas only on HTTP 400/422.
            schemas=("integrated","Aliyun","Anthropic","Ollama",
                     "OpenRouter","OpenAI","XAI","base")
            # As in TranslateAPI.cs, retain the last compatible schema across
            # subsequent requests instead of restarting at "integrated".
            for _ in range(len(schemas)):
                index=self._openai_fallback_index
                data=self._request_data(schemas[index],model,messages,temperature)
                try:
                    reply=self._r(url,data=data,headers=headers)
                    break
                except (ValueError,HTTPError) as error:
                    code=getattr(error,"code",None)
                    if code is None:
                        found=re.search(r"\b(400|422)\b",str(error))
                        code=int(found.group(1)) if found else None
                    if code not in (400,422):
                        raise
                    self._openai_fallback_index+=1
                    if self._openai_fallback_index>=len(schemas):
                        self._openai_fallback_index=0
                        raise
        return THINK.sub("",reply["choices"][0]["message"]["content"])

