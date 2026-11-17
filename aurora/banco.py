"""Dados do condomínio em SQLite.

Os arquivos de dados/ são só o estado inicial: a API nunca escreve neles.
Reservas, visitantes e o vínculo sessão -> apartamento ficam em var/aurora.db.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from aurora.config import ARQUIVO_BANCO, PASTA_DADOS

ESQUEMA = """
CREATE TABLE IF NOT EXISTS reservas (
    codigo       TEXT PRIMARY KEY,
    apartamento  TEXT NOT NULL,
    area         TEXT NOT NULL,
    data         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'ativa' CHECK (status IN ('ativa', 'cancelada')),
    criada_em    TEXT NOT NULL,
    cancelada_em TEXT
);

-- Garantia 5: no máximo uma reserva ativa por área e data, conferido pelo
-- próprio SQLite no INSERT. Duas aprovações simultâneas não passam juntas.
CREATE UNIQUE INDEX IF NOT EXISTS reserva_ativa_unica
    ON reservas (area, data) WHERE status = 'ativa';

-- Todo código já emitido, inclusive de reserva cancelada ou apagada pela
-- restauração. A restauração não limpa esta tabela, então um código nunca
-- volta a ser usado.
CREATE TABLE IF NOT EXISTS codigos_emitidos (
    codigo TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS visitantes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento TEXT NOT NULL,
    nome        TEXT NOT NULL,
    data        TEXT NOT NULL,
    criado_em   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessoes (
    session_id  TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL,
    criada_em   TEXT NOT NULL
);
"""


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conexao() -> Iterator[sqlite3.Connection]:
    ARQUIVO_BANCO.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ARQUIVO_BANCO, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 30000")
    try:
        yield con
    finally:
        con.close()


@contextmanager
def transacao() -> Iterator[sqlite3.Connection]:
    """Transação com BEGIN IMMEDIATE: o lock de escrita é pego no início."""
    with conexao() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
        except BaseException:
            con.execute("ROLLBACK")
            raise
        else:
            con.execute("COMMIT")


def _ler_json(nome: str) -> list[dict]:
    return json.loads((PASTA_DADOS / nome).read_text(encoding="utf-8"))


def apartamentos() -> list[dict]:
    return _ler_json("apartamentos.json")


def areas() -> list[dict]:
    return _ler_json("areas.json")


def apartamento_existe(numero: str) -> bool:
    return any(a["numero"] == numero for a in apartamentos())


def inicializar() -> None:
    """Cria o esquema e, se o banco é novo, carrega o estado inicial."""
    with conexao() as con:
        con.executescript(ESQUEMA)
        vazio = con.execute("SELECT COUNT(*) FROM codigos_emitidos").fetchone()[0] == 0
    if vazio:
        restaurar()


def restaurar() -> None:
    """Volta reservas e visitantes ao estado de dados/ e apaga os vínculos de sessão."""
    reservas = _ler_json("reservas.json")
    visitantes = _ler_json("visitantes.json")
    with conexao() as con:
        con.executescript(ESQUEMA)
    with transacao() as con:
        con.execute("DELETE FROM reservas")
        con.execute("DELETE FROM visitantes")
        con.execute("DELETE FROM sessoes")
        agora = _agora()
        for r in reservas:
            con.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data, criada_em) VALUES (?, ?, ?, ?, ?)",
                (r["codigo"], r["apartamento"], r["area"], r["data"], agora),
            )
            con.execute("INSERT OR IGNORE INTO codigos_emitidos (codigo) VALUES (?)", (r["codigo"],))
        for v in visitantes:
            con.execute(
                "INSERT INTO visitantes (apartamento, nome, data, criado_em) VALUES (?, ?, ?, ?)",
                (v["apartamento"], v["nome"], v["data"], agora),
            )


# Sessões --------------------------------------------------------------------


def registrar_sessao(session_id: str, apartamento: str) -> None:
    with transacao() as con:
        con.execute(
            "INSERT INTO sessoes (session_id, apartamento, criada_em) VALUES (?, ?, ?)",
            (session_id, apartamento, _agora()),
        )


def apartamento_da_sessao(session_id: str) -> str | None:
    with conexao() as con:
        linha = con.execute("SELECT apartamento FROM sessoes WHERE session_id = ?", (session_id,)).fetchone()
    return linha["apartamento"] if linha else None


# Reservas -------------------------------------------------------------------


def reservas_do_apartamento(apartamento: str) -> list[dict]:
    with conexao() as con:
        linhas = con.execute(
            "SELECT codigo, area, data FROM reservas WHERE apartamento = ? AND status = 'ativa' ORDER BY data, area",
            (apartamento,),
        ).fetchall()
    return [dict(l) for l in linhas]


def dono_da_data(area: str, data: str) -> str | None:
    """Apartamento com reserva ativa na área e data. Uso interno das tools: nunca vai para o modelo."""
    with conexao() as con:
        linha = con.execute(
            "SELECT apartamento FROM reservas WHERE area = ? AND data = ? AND status = 'ativa'", (area, data)
        ).fetchone()
    return linha["apartamento"] if linha else None


def _novo_codigo(con: sqlite3.Connection) -> str:
    while True:
        codigo = f"RSV-{secrets.randbelow(900000) + 100000}"
        try:
            con.execute("INSERT INTO codigos_emitidos (codigo) VALUES (?)", (codigo,))
            return codigo
        except sqlite3.IntegrityError:
            continue


def criar_reserva(apartamento: str, area: str, data: str) -> str | None:
    """Grava a reserva e devolve o código, ou None se a área já está ocupada na data.

    A exclusividade não vem de uma consulta anterior: é o índice único
    reserva_ativa_unica que recusa o INSERT quando já existe reserva ativa.
    """
    try:
        with transacao() as con:
            codigo = _novo_codigo(con)
            con.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data, criada_em) VALUES (?, ?, ?, ?, ?)",
                (codigo, apartamento, area, data, _agora()),
            )
            return codigo
    except sqlite3.IntegrityError:
        return None


def cancelar_reserva(apartamento: str, area: str, data: str) -> str | None:
    """Cancela a reserva ativa do próprio apartamento. Devolve o código ou None se não houver."""
    with transacao() as con:
        linha = con.execute(
            "SELECT codigo FROM reservas WHERE apartamento = ? AND area = ? AND data = ? AND status = 'ativa'",
            (apartamento, area, data),
        ).fetchone()
        if not linha:
            return None
        con.execute(
            "UPDATE reservas SET status = 'cancelada', cancelada_em = ? WHERE codigo = ?", (_agora(), linha["codigo"])
        )
        return linha["codigo"]


# Visitantes -----------------------------------------------------------------


def visitantes_do_apartamento(apartamento: str) -> list[dict]:
    with conexao() as con:
        linhas = con.execute(
            "SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY data, id", (apartamento,)
        ).fetchall()
    return [dict(l) for l in linhas]


def visitante_autorizado(apartamento: str, nome: str, data: str) -> bool:
    with conexao() as con:
        linha = con.execute(
            "SELECT 1 FROM visitantes WHERE apartamento = ? AND lower(nome) = lower(?) AND data = ?",
            (apartamento, nome, data),
        ).fetchone()
    return linha is not None


def autorizar_visitante(apartamento: str, nome: str, data: str) -> None:
    with transacao() as con:
        con.execute(
            "INSERT INTO visitantes (apartamento, nome, data, criado_em) VALUES (?, ?, ?, ?)",
            (apartamento, nome, data, _agora()),
        )
