from graylog_mcp.config import StacktraceConfig
from graylog_mcp.shaping import compact_stacktrace

APP = StacktraceConfig(app_packages=("com.acme", "myapp/"), max_frames=3)
NOAPP = StacktraceConfig(max_frames=3)

JAVA = "\n".join(
    [
        "java.lang.IllegalStateException: boom",
        "\tat org.lib.Inner.fail(Inner.java:1)",
        *[f"\tat org.springframework.Proxy{i}.invoke(Proxy.java:{i})" for i in range(10)],
        "\tat com.acme.Service.run(Service.java:42)",
        *[f"\tat org.apache.Valve{i}.invoke(Valve.java:{i})" for i in range(10)],
        "Caused by: java.io.IOException: disk",
        "\tat java.io.File.write(File.java:1)",
        "\tat com.acme.Store.save(Store.java:7)",
        "\t... 21 more",
    ]
)


def test_java_with_app_packages():
    out = compact_stacktrace(JAVA, APP)
    assert "java.lang.IllegalStateException: boom" in out
    assert "org.lib.Inner.fail" in out  # top frame kept
    assert "com.acme.Service.run" in out
    assert "Caused by: java.io.IOException: disk" in out
    assert "com.acme.Store.save" in out
    assert "... 21 more" in out
    assert "Proxy3" not in out and "Valve5" not in out
    assert "… 10 frames" in out
    assert len(out) < len(JAVA) / 2


def test_java_without_app_packages_keeps_first_frames():
    out = compact_stacktrace(JAVA, NOAPP)
    assert "org.lib.Inner.fail" in out and "Proxy1" in out
    assert "Proxy2" not in out
    assert "Caused by: java.io.IOException: disk" in out


PYTHON = """Traceback (most recent call last):
  File "/usr/lib/python3/site-packages/flask/app.py", line 1, in wsgi
    return self.handle()
  File "/usr/lib/python3/site-packages/flask/app.py", line 2, in handle
    return view()
  File "/usr/lib/python3/site-packages/flask/app.py", line 3, in dispatch
    return f()
  File "/usr/lib/python3/site-packages/flask/app.py", line 4, in inner
    return g()
  File "/srv/myapp/views.py", line 10, in order
    total = compute(x)
  File "/usr/lib/python3/site-packages/decimal.py", line 5, in compute
    raise ValueError("bad")
ValueError: bad"""


def test_python_keeps_raise_site_and_app_frames():
    out = compact_stacktrace(PYTHON, APP)
    assert out.startswith("Traceback (most recent call last):")
    assert "/srv/myapp/views.py" in out and "total = compute(x)" in out
    assert 'raise ValueError("bad")' in out  # last frame = where it was raised
    assert "line 2, in handle" not in out
    assert out.endswith("ValueError: bad")


def test_python_without_app_packages_keeps_last_frames():
    out = compact_stacktrace(PYTHON, NOAPP)
    assert "line 1, in wsgi" not in out
    assert "line 4, in inner" in out and "decimal.py" in out


DOTNET = "\n".join(
    [
        "System.InvalidOperationException: nope",
        *[f"   at Microsoft.AspNetCore.Mw{i}.Invoke(HttpContext ctx) in /src/Mw.cs:line {i}" for i in range(8)],
        "   at Acme.Orders.Api.Post() in /src/Api.cs:line 5",
        " ---> System.TimeoutException: slow",
        "   --- End of inner exception stack trace ---",
    ]
)


def test_dotnet():
    cfg = StacktraceConfig(app_packages=("Acme.",), max_frames=3)
    out = compact_stacktrace(DOTNET, cfg)
    assert "Acme.Orders.Api.Post" in out and "Mw0" in out and "Mw4" not in out
    assert "---> System.TimeoutException: slow" in out


GO = """panic: runtime error: index out of range

goroutine 1 [running]:
github.com/acme/svc/handler.(*H).Serve(0xc000010000)
\t/src/handler.go:42 +0x1d
net/http.serverHandler.ServeHTTP(0x1)
\t/usr/local/go/src/net/http/server.go:2879 +0x43
net/http.(*conn).serve(0x2)
\t/usr/local/go/src/net/http/server.go:1930 +0xb08
net/http.(*conn).other(0x2)
\t/usr/local/go/src/net/http/server.go:1931 +0xb08
created by net/http.(*Server).Serve
\t/usr/local/go/src/net/http/server.go:3034 +0x4e8"""


def test_go():
    cfg = StacktraceConfig(app_packages=("github.com/acme",), max_frames=2)
    out = compact_stacktrace(GO, cfg)
    assert out.startswith("panic: runtime error")
    assert "github.com/acme/svc/handler" in out and "/src/handler.go:42" in out
    assert "server.go:2879" not in out
    assert "… 4 frames" in out


NODE = "\n".join(
    [
        "TypeError: Cannot read properties of undefined (reading 'id')",
        "    at getUser (/app/src/users.js:10:15)",
        *[f"    at Layer.handle (/app/node_modules/express/lib/router/layer.js:{i}:5)" for i in range(8)],
    ]
)


def test_node():
    cfg = StacktraceConfig(app_packages=("/app/src/",), max_frames=2)
    out = compact_stacktrace(NODE, cfg)
    assert "getUser" in out and "layer.js:3" not in out and "… 8 frames" in out


def test_plain_text_untouched():
    text = "line one\nline two\n  indented"
    assert compact_stacktrace(text, APP) == text
