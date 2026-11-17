"""Leitura do regulamento por capítulo.

O texto nunca é carregado inteiro em instrução nenhuma: o especialista de
regulamento vê a lista de títulos e lê só o capítulo que precisa.
"""

from __future__ import annotations

import re
from functools import cache

from aurora.config import PASTA_DADOS

_TITULO = re.compile(r"^## Capítulo ([IVXLC]+): (.+)$")


@cache
def capitulos() -> dict[str, dict[str, str]]:
    """{numero_romano: {"titulo": ..., "texto": ...}} na ordem do arquivo."""
    resultado: dict[str, dict[str, str]] = {}
    atual: str | None = None
    linhas: list[str] = []
    for linha in (PASTA_DADOS / "regulamento.md").read_text(encoding="utf-8").splitlines():
        m = _TITULO.match(linha)
        if m:
            if atual:
                resultado[atual]["texto"] = "\n".join(linhas).strip()
            atual, linhas = m.group(1), [linha]
            resultado[atual] = {"titulo": m.group(2), "texto": ""}
        elif atual:
            linhas.append(linha)
    if atual:
        resultado[atual]["texto"] = "\n".join(linhas).strip()
    return resultado
