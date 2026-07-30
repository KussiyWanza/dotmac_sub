from __future__ import annotations

from pathlib import Path

from tests.architecture.source_index import (
    call_lines,
    class_names,
    clear_source_index,
    files,
    identifier_names,
    python_ast,
    python_files,
    python_nodes,
    source_text,
    string_constants,
)


def test_source_index_reuses_file_lists_text_and_trees(tmp_path: Path) -> None:
    clear_source_index()
    source = tmp_path / "sample.py"
    source.write_text(
        'class Example:\n    pass\n\nVALUE = build("one")\n',
        encoding="utf-8",
    )

    listed = python_files(tmp_path)
    text = source_text(source)
    tree = python_ast(source)
    nodes = python_nodes(source)

    source.write_text("VALUE = 2\n", encoding="utf-8")

    assert listed == (source,)
    assert files(tmp_path, "*.py") is listed
    assert source_text(source) is text
    assert python_ast(source) is tree
    assert python_nodes(source) is nodes
    assert string_constants(source) == frozenset({"one"})
    assert identifier_names(source) >= {"VALUE", "build"}
    assert class_names(source) == frozenset({"Example"})
    assert call_lines(source) == {"build": (4,)}

    clear_source_index()
    assert source_text(source) == "VALUE = 2\n"
