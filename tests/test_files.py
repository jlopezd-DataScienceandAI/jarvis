from __future__ import annotations

import os
import time

import pytest

from jarvis.brain.tools import ToolError, registry
from jarvis.brain.tools.files import (
    confirm_move_question,
    move_or_rename,
    read_file,
    resolve_allowed,
    search_files,
    write_file,
)


def test_path_inside_root_is_allowed(config, home):
    assert resolve_allowed(str(home / "docs" / "a.txt"), config) == (home / "docs" / "a.txt").resolve()


@pytest.mark.parametrize("escape", ["..\\..\\Windows\\system32\\drivers\\etc\\hosts", "C:\\Windows\\win.ini"])
def test_path_outside_root_is_refused(config, home, escape):
    with pytest.raises(ToolError, match="fuera de las carpetas permitidas"):
        resolve_allowed(str(home / escape) if not escape.startswith("C:") else escape, config)


@pytest.mark.parametrize("protected", ["AppData\\Roaming\\x.json", ".ssh\\id_rsa", "proyecto\\.env"])
def test_protected_paths_are_refused_even_inside_root(config, home, protected):
    with pytest.raises(ToolError, match="protegida"):
        resolve_allowed(str(home / protected), config)


def test_search_matches_accent_insensitively_and_sorts_by_date(config, home):
    old = home / "Factura luz octubre.pdf"
    new = home / "docs" / "FACTURA_LUZ_noviembre.pdf"
    new.parent.mkdir()
    for path in (old, new, home / "factura agua.pdf", home / "luz.txt"):
        path.write_text("x", encoding="utf-8")
    past = time.time() - 40 * 86400
    os.utime(old, (past, past))

    result = search_files(config, "factura luz", extension="pdf")
    lines = result.splitlines()
    assert lines[0].startswith("2 resultado(s)")
    assert "noviembre" in lines[1] and "octubre" in lines[2]

    recent = search_files(config, "factura luz", extension="pdf", modified_within_days=31)
    assert "noviembre" in recent and "octubre" not in recent
    assert search_files(config, "inexistente") == "Sin resultados."


def test_search_skips_protected_and_noise_folders(config, home):
    for folder in ("AppData", "node_modules", ".git"):
        (home / folder).mkdir()
        (home / folder / "secreto.txt").write_text("x", encoding="utf-8")
    assert search_files(config, "secreto") == "Sin resultados."


def test_read_text_and_truncation_is_announced(config, home):
    path = home / "nota.txt"
    path.write_text("á" * 50, encoding="utf-8")
    assert read_file(config, str(path)) == "á" * 50
    truncated = read_file(config, str(path), max_chars=10)
    assert truncated.startswith("á" * 10 + "\n[Truncado")


def test_read_refuses_binary_types(config, home):
    path = home / "foto.png"
    path.write_bytes(b"\x89PNG")
    with pytest.raises(ToolError, match="No sé leer"):
        read_file(config, str(path))


def test_rename_and_move_into_folder(config, home):
    src = home / "a.txt"
    src.write_text("hola", encoding="utf-8")
    move_or_rename(config, str(src), str(home / "b.txt"))
    assert not src.exists() and (home / "b.txt").read_text(encoding="utf-8") == "hola"

    (home / "Impuestos").mkdir()
    move_or_rename(config, str(home / "b.txt"), str(home / "Impuestos"))
    assert (home / "Impuestos" / "b.txt").exists()


def test_move_never_overwrites(config, home):
    (home / "a.txt").write_text("nuevo", encoding="utf-8")
    (home / "b.txt").write_text("valioso", encoding="utf-8")
    with pytest.raises(ToolError, match="Ya existe"):
        move_or_rename(config, str(home / "a.txt"), str(home / "b.txt"))
    assert (home / "b.txt").read_text(encoding="utf-8") == "valioso"


def test_move_out_of_root_is_refused(config, home, tmp_path):
    (home / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ToolError):
        move_or_rename(config, str(home / "a.txt"), str(tmp_path / "fuera.txt"))
    assert (home / "a.txt").exists()


def test_write_refuses_to_replace_unless_asked(config, home):
    target = home / "tabla.md"
    write_file(config, str(target), "v1")
    with pytest.raises(ToolError, match="Ya existe"):
        write_file(config, str(target), "v2")
    write_file(config, str(target), "v2", overwrite=True)
    assert target.read_text(encoding="utf-8") == "v2"


def test_move_tool_asks_and_respects_a_no(ctx, home, answers):
    (home / "a.txt").write_text("x", encoding="utf-8")
    args = {"source": str(home / "a.txt"), "destination": str(home / "b.txt")}

    answers.append(False)
    declined = registry.call("move_or_rename", args, ctx)
    assert "no ha confirmado" in declined.content and (home / "a.txt").exists()
    assert ctx.asked == ["¿Confirma que renombro a.txt como b.txt, señor?"]

    answers.append(True)
    assert not registry.call("move_or_rename", args, ctx).is_error
    assert (home / "b.txt").exists()


def test_write_tool_only_asks_when_overwriting(ctx, home, answers):
    args = {"path": str(home / "t.md"), "content": "v1"}
    assert not registry.call("write_file", args, ctx).is_error
    assert ctx.asked == []

    answers.append(False)
    registry.call("write_file", {**args, "content": "v2", "overwrite": True}, ctx)
    assert len(ctx.asked) == 1 and (home / "t.md").read_text(encoding="utf-8") == "v1"


def test_move_question_describes_folder_moves():
    question = confirm_move_question(r"C:\u\Descargas\factura.pdf", r"C:\u\Documentos\Impuestos", "señor")
    assert question == "¿Confirma que muevo factura.pdf a la carpeta Impuestos, señor?"
    renamed = confirm_move_question(r"C:\u\Descargas\f.pdf", r"C:\u\Impuestos\2026-11-14 luz.pdf", "señor")
    assert renamed == "¿Confirma que muevo f.pdf a la carpeta Impuestos con el nombre 2026-11-14 luz.pdf, señor?"
