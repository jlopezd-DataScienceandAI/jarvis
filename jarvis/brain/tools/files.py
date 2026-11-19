"""File tools: search, read, move/rename and write, confined to allowed folders."""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from jarvis.config import Config
from jarvis.text import words

from .registry import ToolContext, ToolError, registry

# Folder names never entered or touched, even inside an allowed root.
_DENIED_PARTS = {
    "appdata", ".ssh", ".aws", ".azure", ".gnupg", ".kube", ".docker", ".claude",
    "$recycle.bin", "system volume information",
}
_DENIED_NAMES = {".env", "ntuser.dat", "id_rsa", "id_ed25519"}
# Skipped while searching: huge, noisy, and never what the user is looking for.
_SKIP_DIRS = _DENIED_PARTS | {"node_modules", ".git", ".venv", "venv", "__pycache__", ".cache"}

_TEXT_SUFFIXES = {
    ".txt", ".md", ".csv", ".json", ".log", ".ini", ".toml", ".yaml", ".yml", ".xml", ".html",
    ".py", ".js", ".ts", ".css", ".bat", ".ps1", ".sh", ".tex", ".srt", ".rtf",
}


def allowed_roots(config: Config) -> list[Path]:
    roots = [Path(os.path.expandvars(r)).expanduser() for r in config.files.allowed_roots]
    return [r.resolve() for r in roots] or [Path.home().resolve()]


def resolve_allowed(raw: str, config: Config) -> Path:
    """Resolve a model-supplied path and refuse anything outside the allowed roots.

    The path is untrusted model output: it is fully resolved (``..``, symlinks,
    environment variables) before the check, so traversal cannot escape.
    """
    if not raw or not raw.strip():
        raise ToolError("Falta la ruta.")
    path = Path(os.path.expandvars(raw.strip().strip('"'))).expanduser()
    if not path.is_absolute():
        path = Path.home() / path
    path = path.resolve()
    roots = allowed_roots(config)
    root = next((r for r in roots if path == r or path.is_relative_to(r)), None)
    if root is None:
        raise ToolError(
            f"La ruta {path} está fuera de las carpetas permitidas ({', '.join(str(r) for r in roots)})."
        )
    # Only the part below the root is judged: a root the user chose is allowed as a whole.
    lowered = {part.lower() for part in path.relative_to(root).parts}
    if lowered & _DENIED_PARTS or path.name.lower() in _DENIED_NAMES:
        raise ToolError(f"La ruta {path} está protegida y no puedo tocarla.")
    return path


def _format_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def search_files(
    config: Config,
    query: str = "",
    folder: str | None = None,
    extension: str | None = None,
    modified_within_days: int | None = None,
    max_results: int = 20,
    time_budget_s: float = 10.0,
) -> str:
    bases = [resolve_allowed(folder, config)] if folder else allowed_roots(config)
    tokens = words(query)
    suffix = ("." + extension.lower().lstrip(".")) if extension else None
    newer_than = time.time() - modified_within_days * 86400 if modified_within_days else None
    deadline = time.monotonic() + time_budget_s
    hits: list[tuple[float, int, str]] = []
    timed_out = False

    for base in bases:
        if not base.is_dir():
            raise ToolError(f"No existe la carpeta {base}.")
        for dirpath, dirnames, filenames in os.walk(base):
            if time.monotonic() > deadline:
                timed_out = True
                break
            dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS and not d.startswith(".")]
            for filename in filenames:
                if suffix and not filename.lower().endswith(suffix):
                    continue
                if filename.lower() in _DENIED_NAMES:
                    continue
                haystack = " ".join(words(filename))
                if not all(token in haystack for token in tokens):
                    continue
                full = os.path.join(dirpath, filename)
                try:
                    stat = os.stat(full)
                except OSError:
                    continue
                if newer_than and stat.st_mtime < newer_than:
                    continue
                hits.append((stat.st_mtime, stat.st_size, full))

    if not hits:
        note = " La búsqueda se cortó por tiempo; prueba con una carpeta concreta." if timed_out else ""
        return "Sin resultados." + note
    hits.sort(reverse=True)
    lines = [
        f"{path} | {_format_size(size)} | modificado {datetime.fromtimestamp(mtime):%Y-%m-%d %H:%M}"
        for mtime, size, path in hits[:max_results]
    ]
    header = f"{len(hits)} resultado(s), del más reciente al más antiguo"
    if len(hits) > max_results:
        header += f" (mostrando {max_results})"
    if timed_out:
        header += "; búsqueda incompleta por tiempo"
    return header + ":\n" + "\n".join(lines)


def read_file(config: Config, path: str, max_chars: int = 20000) -> str:
    target = resolve_allowed(path, config)
    if not target.is_file():
        raise ToolError(f"No existe el archivo {target}.")
    suffix = target.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(target))
        parts: list[str] = []
        total = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            parts.append(text)
            total += len(text)
            if total >= max_chars:
                break
        content = "\n".join(parts).strip()
        if not content:
            raise ToolError("El PDF no contiene texto extraíble (probablemente es un escaneo).")
    elif suffix in _TEXT_SUFFIXES or suffix == "":
        content = target.read_text(encoding="utf-8", errors="replace")
    else:
        raise ToolError(f"No sé leer archivos {suffix}. Puedo leer texto y PDF.")
    if len(content) > max_chars:
        return (content[:max_chars]
                + f"\n[Truncado: se muestran {max_chars} de {len(content)} caracteres o más.]")
    return content


def move_or_rename(config: Config, source: str, destination: str) -> str:
    src = resolve_allowed(source, config)
    dst = resolve_allowed(destination, config)
    if not src.exists():
        raise ToolError(f"No existe {src}.")
    if src in allowed_roots(config):
        raise ToolError("No puedo mover una carpeta raíz permitida.")
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        raise ToolError(f"Ya existe {dst}. No sobrescribo archivos al mover.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"Movido: {src} -> {dst}"


def write_file(config: Config, path: str, content: str, overwrite: bool = False) -> str:
    target = resolve_allowed(path, config)
    if target.is_dir():
        raise ToolError(f"{target} es una carpeta.")
    if target.exists() and not overwrite:
        raise ToolError(f"Ya existe {target}. Usa otro nombre o overwrite=true.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Guardado: {target} ({len(content)} caracteres)"


# ----------------------------------------------------------------- registration


class SearchFilesArgs(BaseModel):
    query: str = Field(
        default="",
        description="Palabras que deben aparecer en el nombre del archivo (sin importar acentos ni "
        "mayúsculas). Vacío lista los archivos más recientes.",
    )
    folder: str | None = Field(default=None, description="Carpeta donde buscar. Por defecto, todas las permitidas.")
    extension: str | None = Field(default=None, description="Extensión sin punto, p. ej. 'pdf'.")
    modified_within_days: int | None = Field(
        default=None, ge=1, description="Solo archivos modificados en los últimos N días."
    )


class ReadFileArgs(BaseModel):
    path: str = Field(description="Ruta del archivo de texto o PDF.")


class MoveArgs(BaseModel):
    source: str = Field(description="Ruta actual del archivo o carpeta.")
    destination: str = Field(
        description="Ruta nueva completa (para renombrar, misma carpeta con otro nombre), o una "
        "carpeta existente a la que moverlo conservando el nombre."
    )


class WriteFileArgs(BaseModel):
    path: str = Field(description="Ruta del archivo a crear, con extensión (.txt, .md, .csv...).")
    content: str = Field(description="Contenido de texto completo.")
    overwrite: bool = Field(default=False, description="Reemplazar el archivo si ya existe.")


@registry.tool(
    "search_files",
    "Busca archivos por nombre en las carpetas del usuario. Devuelve ruta, tamaño y fecha de "
    "modificación. Si no hay resultados, prueba con menos palabras o sinónimos.",
    SearchFilesArgs,
)
def _search_files(args: SearchFilesArgs, ctx: ToolContext) -> str:
    return search_files(ctx.config, args.query, args.folder, args.extension, args.modified_within_days)


@registry.tool("read_file", "Lee el contenido de un archivo de texto o PDF.", ReadFileArgs)
def _read_file(args: ReadFileArgs, ctx: ToolContext) -> str:
    return read_file(ctx.config, args.path)


@registry.tool(
    "move_or_rename",
    "Mueve o renombra un archivo o carpeta. Nunca sobrescribe un destino existente.",
    MoveArgs,
    confirm=lambda args, ctx: confirm_move_question(args.source, args.destination, ctx.config.user.address),
)
def _move_or_rename(args: MoveArgs, ctx: ToolContext) -> str:
    return move_or_rename(ctx.config, args.source, args.destination)


def confirm_move_question(source: str, destination: str, address: str) -> str:
    """Short spoken description of a move, so the user knows what they are approving."""
    src, dst = Path(source), Path(destination)
    if src.parent == dst.parent:
        return f"¿Confirma que renombro {src.name} como {dst.name}, {address}?"
    folder = dst if dst.suffix == "" else dst.parent
    renamed = "" if dst.suffix == "" or dst.name == src.name else f" con el nombre {dst.name}"
    return f"¿Confirma que muevo {src.name} a la carpeta {folder.name}{renamed}, {address}?"


def _confirm_overwrite(args: WriteFileArgs, ctx: ToolContext) -> str | None:
    if not args.overwrite:
        return None
    try:
        exists = resolve_allowed(args.path, ctx.config).exists()
    except ToolError:
        return None  # the tool itself will report the bad path
    return f"¿Confirma que sobrescribo {Path(args.path).name}, {ctx.config.user.address}?" if exists else None


@registry.tool(
    "write_file",
    "Crea un archivo de texto con el contenido dado (notas, tablas en Markdown o CSV, informes).",
    WriteFileArgs,
    confirm=_confirm_overwrite,
)
def _write_file(args: WriteFileArgs, ctx: ToolContext) -> str:
    return write_file(ctx.config, args.path, args.content, args.overwrite)
