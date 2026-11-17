"""Volta reservas e visitantes ao estado de dados/ e apaga as sessões.

Rode com a API parada. Os códigos de reserva já emitidos continuam
registrados, então nenhuma reserva nova repete um código antigo.
"""

import sqlite3

from aurora import banco
from aurora.config import ARQUIVO_SESSOES


def main() -> None:
    banco.restaurar()
    if ARQUIVO_SESSOES.exists():
        with sqlite3.connect(ARQUIVO_SESSOES) as con:
            tabelas = [t for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
            for tabela in ("events", "sessions", "app_states", "user_states"):
                if tabela in tabelas:
                    con.execute(f"DELETE FROM {tabela}")
    print("Dados restaurados a partir de dados/ e sessões apagadas.")


if __name__ == "__main__":
    main()
