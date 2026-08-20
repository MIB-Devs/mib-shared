from pathlib import Path

from mib_shared.lint import check_paths, check_source, main

HERE = Path("sample.py")


def test_untimed_module_level_call_is_flagged():
    findings = check_source("import httpx\nhttpx.get('http://svc/thing')\n", HERE)
    assert len(findings) == 1
    assert "no timeout" in findings[0].message
    assert findings[0].line == 2


def test_timed_call_is_accepted():
    assert check_source("import httpx\nhttpx.get('http://x', timeout=2)\n", HERE) == []


def test_untimed_client_construction_is_flagged():
    src = "import httpx\nc = httpx.AsyncClient(base_url='http://svc')\n"
    assert len(check_source(src, HERE)) == 1


def test_timed_client_construction_is_accepted():
    src = "import httpx\nc = httpx.AsyncClient(base_url='http://svc', timeout=5)\n"
    assert check_source(src, HERE) == []


def test_requests_is_covered_too():
    # requests has no default timeout at all, which is worse than a long one.
    assert len(check_source("import requests\nrequests.post('http://x')\n", HERE)) == 1


def test_kwargs_forwarding_is_not_flagged():
    # A wrapper passing the caller's timeout through would otherwise train
    # people to suppress the check.
    src = "import httpx\ndef f(**kw):\n    return httpx.get('http://x', **kw)\n"
    assert check_source(src, HERE) == []


def test_an_explicit_suppression_comment_is_honoured():
    src = "import httpx\nhttpx.get('http://x')  # mib: timeout-ok\n"
    assert check_source(src, HERE) == []


def test_unrelated_calls_are_ignored():
    src = "import os\nos.get_terminal_size()\nclient.get('/thing')\n"
    assert check_source(src, HERE) == []


def test_a_syntax_error_is_reported_not_swallowed():
    findings = check_source("def broken(\n", HERE)
    assert len(findings) == 1
    assert "could not parse" in findings[0].message


def test_the_library_itself_passes_its_own_check():
    # mib_shared.http_client always sets a timeout; if that regresses, this fails.
    assert check_paths(["src"]) == []


def test_main_exits_nonzero_on_a_finding(tmp_path, capsys):
    bad = tmp_path / "svc.py"
    bad.write_text("import httpx\nhttpx.get('http://x')\n", encoding="utf-8")
    assert main([str(tmp_path)]) == 1
    assert "no timeout" in capsys.readouterr().err


def test_main_exits_zero_when_clean(tmp_path, capsys):
    good = tmp_path / "svc.py"
    good.write_text("import httpx\nhttpx.get('http://x', timeout=1)\n", encoding="utf-8")
    assert main([str(tmp_path)]) == 0
    assert "OK" in capsys.readouterr().out
