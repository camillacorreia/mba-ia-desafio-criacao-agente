"""Caminhos e variáveis de ambiente."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")

PASTA_DADOS = RAIZ / "dados"
PASTA_VAR = RAIZ / "var"
ARQUIVO_BANCO = PASTA_VAR / "aurora.db"
ARQUIVO_SESSOES = PASTA_VAR / "sessoes.db"

NOME_APP = "residencial_aurora"

MODELO_PRINCIPAL = os.getenv("MODELO_PRINCIPAL") or "gemini-3.5-flash"
MODELO_ESPECIALISTAS = os.getenv("MODELO_ESPECIALISTAS") or "gemini-3.5-flash"

# Chave da sessão onde fica o apartamento do morador autenticado. Só a rota
# POST /sessoes escreve aqui; nenhuma tool escreve no state.
CHAVE_APARTAMENTO = "apartamento"
