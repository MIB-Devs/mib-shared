from pathlib import Path

from mib_shared.comments import check_source, main

HERE = Path("sample.py")


def test_a_comment_in_a_function_body_is_flagged():
    findings = check_source("def f():\n    # why\n    return 1\n", HERE)
    assert [f.line for f in findings] == [2]


def test_a_trailing_comment_is_flagged():
    assert len(check_source("def f():\n    return 1  # one\n", HERE)) == 1


def test_a_comment_closing_the_body_is_flagged():
    src = "def f():\n    x = 1\n    # tail\n\ny = 2\n"
    assert [f.line for f in check_source(src, HERE)] == [3]


def test_a_method_and_a_nested_function_are_covered():
    src = "class A:\n    def m(self):\n        def g():\n            # n\n            pass\n"
    assert len(check_source(src, HERE)) == 1


def test_module_and_class_level_comments_are_allowed():
    src = "# top\nclass A:\n    # about x\n    x = 1\n\n\n# before f\ndef f():\n    return 1\n"
    assert check_source(src, HERE) == []


def test_tool_directives_are_allowed():
    src = (
        "def f(x):\n"
        "    y = g(x)  # type: ignore[attr-defined]\n"
        "    import os  # noqa: F401\n"
        "    return y  # pragma: no cover\n"
    )
    assert check_source(src, HERE) == []


def test_a_hash_inside_a_string_is_not_a_comment():
    assert check_source('def f():\n    return "# not a comment"\n', HERE) == []


def test_a_syntax_error_is_reported_not_swallowed():
    assert "could not parse" in check_source("def broken(\n", HERE)[0].message


def test_main_exit_codes(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def f():\n    # x\n    pass\n")
    assert main([str(bad)]) == 1
    bad.write_text('def f():\n    """x."""\n')
    assert main([str(bad)]) == 0


def test_the_library_itself_passes_its_own_check():
    assert main(["src"]) == 0
