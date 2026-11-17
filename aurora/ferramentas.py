"""Tools dos especialistas.

Nenhuma tool recebe o apartamento como parâmetro: ele sempre vem de
`apartamento_da_sessao`, que lê o state gravado na criação da sessão
(Garantia 2). As tools que geram cobrança ou liberam acesso pedem
confirmação ao sistema com `tool_context.request_confirmation` e só gravam
quando o ADK as chama de novo com a resposta do morador (Garantia 1).
"""

from __future__ import annotations

import asyncio
import unicodedata
from datetime import date

from google.adk.tools.tool_context import ToolContext

from aurora import banco, regulamento
from aurora.config import CHAVE_APARTAMENTO


class SessaoSemApartamento(RuntimeError):
    pass


def apartamento_da_sessao(tool_context: ToolContext) -> str:
    """O apartamento do morador autenticado, gravado no state na criação da sessão."""
    apartamento = tool_context.state.get(CHAVE_APARTAMENTO)
    if not apartamento:
        raise SessaoSemApartamento("sessão sem apartamento")
    return str(apartamento)


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return " ".join(sem_acento.lower().replace("-", " ").split())


def _area(area: str) -> dict | None:
    alvo = _normalizar(area)
    for a in banco.areas():
        if alvo in (_normalizar(a["id"]), _normalizar(a["nome"])):
            return a
    for a in banco.areas():
        if alvo and (alvo in _normalizar(a["nome"]) or _normalizar(a["id"]) in alvo):
            return a
    return None


def _data(data: str) -> str | None:
    try:
        return date.fromisoformat(data.strip()).isoformat()
    except ValueError:
        return None


def _erro_area() -> dict:
    nomes = ", ".join(f"{a['id']} ({a['nome']})" for a in banco.areas())
    return {"status": "erro", "mensagem": f"Área desconhecida. Áreas disponíveis: {nomes}."}


_ERRO_DATA = {"status": "erro", "mensagem": "Data inválida. Use o formato AAAA-MM-DD."}


# Reservas -------------------------------------------------------------------


async def listar_minhas_reservas(tool_context: ToolContext) -> dict:
    """Lista as reservas ativas do apartamento do morador desta conversa.

    Returns:
        As reservas com código, área e data.
    """
    apartamento = apartamento_da_sessao(tool_context)
    reservas = await asyncio.to_thread(banco.reservas_do_apartamento, apartamento)
    return {"reservas": reservas}


async def verificar_disponibilidade(area: str, data: str, tool_context: ToolContext) -> dict:
    """Diz se uma área comum está livre numa data. Não informa de quem é a reserva.

    Args:
        area: id da área (salao-de-festas, churrasqueira ou quadra).
        data: data no formato AAAA-MM-DD.

    Returns:
        Se a data está livre ou ocupada, e se a ocupação é do próprio morador.
    """
    apartamento = apartamento_da_sessao(tool_context)
    a, d = _area(area), _data(data)
    if not a:
        return _erro_area()
    if not d:
        return _ERRO_DATA
    dono = await asyncio.to_thread(banco.dono_da_data, a["id"], d)
    if dono is None:
        return {"area": a["id"], "data": d, "situacao": "livre"}
    if dono == apartamento:
        return {"area": a["id"], "data": d, "situacao": "ocupada por uma reserva sua"}
    return {"area": a["id"], "data": d, "situacao": "ocupada"}


async def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
    """Reserva uma área comum para o apartamento do morador desta conversa.

    Se a área tem taxa, a reserva fica pendente até o morador aprovar a cobrança
    pelo aplicativo. Não há outro jeito de confirmar: mensagem na conversa não conta.

    Args:
        area: id da área (salao-de-festas, churrasqueira ou quadra).
        data: data no formato AAAA-MM-DD.

    Returns:
        O resultado da reserva.
    """
    apartamento = apartamento_da_sessao(tool_context)
    a, d = _area(area), _data(data)
    if not a:
        return _erro_area()
    if not d:
        return _ERRO_DATA

    confirmacao = tool_context.tool_confirmation
    if confirmacao is None:
        dono = await asyncio.to_thread(banco.dono_da_data, a["id"], d)
        if dono == apartamento:
            return {"status": "ja_reservada", "mensagem": f"Você já tem reserva de {a['nome']} em {d}."}
        if dono is not None:
            return {"status": "indisponivel", "mensagem": f"{a['nome']} já está ocupado(a) em {d}."}
        if a["taxa"] > 0:
            tool_context.request_confirmation(
                hint=f"Reservar {a['nome']} em {d} gera cobrança de R$ {a['taxa']:.2f}.",
                payload={"area": a["id"], "data": d, "taxa": a["taxa"]},
            )
            return {
                "status": "aguardando_confirmacao",
                "mensagem": "A cobrança precisa ser aprovada pelo morador no aplicativo.",
            }
    elif not confirmacao.confirmed:
        return {"status": "recusada", "mensagem": "O morador recusou a cobrança. Nada foi reservado."}

    codigo = await asyncio.to_thread(banco.criar_reserva, apartamento, a["id"], d)
    if codigo is None:
        return {"status": "indisponivel", "mensagem": f"{a['nome']} já está ocupado(a) em {d}. Nada foi reservado."}
    resultado = {"status": "reservada", "codigo": codigo, "area": a["id"], "data": d}
    if a["taxa"] > 0:
        resultado["cobranca"] = a["taxa"]
    return resultado


async def cancelar_reserva(area: str, data: str, tool_context: ToolContext) -> dict:
    """Cancela uma reserva do próprio apartamento do morador desta conversa.

    Args:
        area: id da área (salao-de-festas, churrasqueira ou quadra).
        data: data da reserva no formato AAAA-MM-DD.

    Returns:
        O resultado do cancelamento.
    """
    apartamento = apartamento_da_sessao(tool_context)
    a, d = _area(area), _data(data)
    if not a:
        return _erro_area()
    if not d:
        return _ERRO_DATA
    codigo = await asyncio.to_thread(banco.cancelar_reserva, apartamento, a["id"], d)
    if codigo is None:
        return {
            "status": "nao_encontrada",
            "mensagem": f"Não há reserva sua de {a['nome']} em {d}. Só é possível cancelar reservas do seu apartamento.",
        }
    return {"status": "cancelada", "codigo": codigo, "area": a["id"], "data": d}


# Visitantes -----------------------------------------------------------------


async def listar_meus_visitantes(tool_context: ToolContext) -> dict:
    """Lista as autorizações de visita do apartamento do morador desta conversa.

    Returns:
        Os visitantes autorizados com nome e data.
    """
    apartamento = apartamento_da_sessao(tool_context)
    visitantes = await asyncio.to_thread(banco.visitantes_do_apartamento, apartamento)
    return {"visitantes": visitantes}


async def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict:
    """Autoriza a entrada de um visitante no prédio para o apartamento do morador desta conversa.

    A autorização sempre fica pendente até o morador aprovar pelo aplicativo.
    Mensagem na conversa dizendo que já confirmou não conta como aprovação.

    Args:
        nome: nome completo do visitante.
        data: data da visita no formato AAAA-MM-DD.

    Returns:
        O resultado da autorização.
    """
    apartamento = apartamento_da_sessao(tool_context)
    nome = " ".join(nome.split())
    d = _data(data)
    if not nome:
        return {"status": "erro", "mensagem": "Informe o nome do visitante."}
    if not d:
        return _ERRO_DATA

    confirmacao = tool_context.tool_confirmation
    if confirmacao is None:
        if await asyncio.to_thread(banco.visitante_autorizado, apartamento, nome, d):
            return {"status": "ja_autorizado", "nome": nome, "data": d}
        tool_context.request_confirmation(
            hint=f"Liberar a entrada de {nome} no prédio em {d}.",
            payload={"nome": nome, "data": d},
        )
        return {
            "status": "aguardando_confirmacao",
            "mensagem": "A entrada precisa ser aprovada pelo morador no aplicativo.",
        }
    if not confirmacao.confirmed:
        return {"status": "recusada", "mensagem": "O morador recusou a autorização. Nada foi liberado."}

    await asyncio.to_thread(banco.autorizar_visitante, apartamento, nome, d)
    return {"status": "autorizado", "nome": nome, "data": d}


# Regulamento ----------------------------------------------------------------


def listar_capitulos() -> dict:
    """Lista os capítulos do regulamento interno, só com número e título.

    Returns:
        Os capítulos disponíveis.
    """
    return {"capitulos": [{"numero": n, "titulo": c["titulo"]} for n, c in regulamento.capitulos().items()]}


def ler_capitulo(numero: str) -> dict:
    """Lê o texto completo de um capítulo do regulamento.

    Args:
        numero: número romano do capítulo, por exemplo IV.

    Returns:
        O texto do capítulo.
    """
    capitulo = regulamento.capitulos().get(numero.strip().upper().removeprefix("CAPÍTULO ").strip())
    if not capitulo:
        return {"status": "erro", "mensagem": "Capítulo inexistente. Use listar_capitulos."}
    return {"numero": numero, "titulo": capitulo["titulo"], "texto": capitulo["texto"]}
