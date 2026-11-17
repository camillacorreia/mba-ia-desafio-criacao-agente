"""API HTTP do assistente do Residencial Aurora."""

from __future__ import annotations

import asyncio
import sqlite3
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.sessions.session import Session
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.genai import types
from pydantic import BaseModel

from aurora import banco
from aurora.agentes import criar_app
from aurora.config import ARQUIVO_SESSOES, CHAVE_APARTAMENTO, NOME_APP

CONFIRMACAO = "adk_request_confirmation"


class NovaSessao(BaseModel):
    apartamento: str


class Mensagem(BaseModel):
    texto: str


class RespostaConfirmacao(BaseModel):
    id: str
    confirmado: bool


def _preparar_banco_de_sessoes() -> None:
    ARQUIVO_SESSOES.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ARQUIVO_SESSOES) as con:
        con.execute("PRAGMA journal_mode = WAL")


@asynccontextmanager
async def lifespan(app: FastAPI):
    banco.inicializar()
    _preparar_banco_de_sessoes()
    servico = SqliteSessionService(str(ARQUIVO_SESSOES))
    app.state.sessoes = servico
    app.state.runner = Runner(app=criar_app(), session_service=servico)
    app.state.locks = defaultdict(asyncio.Lock)
    yield


api = FastAPI(title="Residencial Aurora", lifespan=lifespan)


def _usuario(apartamento: str) -> str:
    return f"apto-{apartamento}"


async def _sessao(session_id: str) -> Session:
    apartamento = await asyncio.to_thread(banco.apartamento_da_sessao, session_id)
    if apartamento is None:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    sessao = await api.state.sessoes.get_session(
        app_name=NOME_APP, user_id=_usuario(apartamento), session_id=session_id
    )
    if sessao is None:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    return sessao


def confirmacoes_pendentes(eventos: list[Event]) -> list[dict[str, Any]]:
    """Pedidos de confirmação da sessão que ainda não têm resposta.

    Lidos dos eventos gravados, não de memória: sobrevivem ao reinício e uma
    confirmação respondida deixa de aparecer porque a resposta vira evento.
    """
    respondidas = {
        p.function_response.id
        for e in eventos
        if e.content and e.content.parts
        for p in e.content.parts
        if p.function_response and p.function_response.name == CONFIRMACAO
    }
    pendentes = []
    for e in eventos:
        for chamada in e.get_function_calls():
            if chamada.name != CONFIRMACAO or chamada.id in respondidas:
                continue
            args = chamada.args or {}
            original = args.get("originalFunctionCall") or {}
            pedido = args.get("toolConfirmation") or {}
            pendentes.append(
                {
                    "id": chamada.id,
                    "acao": original.get("name", ""),
                    "detalhes": pedido.get("payload") or original.get("args") or {},
                }
            )
    return pendentes


async def _executar(sessao: Session, mensagem: types.Content) -> dict[str, Any]:
    textos: list[str] = []
    async for evento in api.state.runner.run_async(
        user_id=sessao.user_id, session_id=sessao.id, new_message=mensagem
    ):
        if evento.author == "user" or evento.partial or not evento.content or not evento.content.parts:
            continue
        textos.extend(p.text for p in evento.content.parts if p.text and not p.thought)
    atualizada = await api.state.sessoes.get_session(
        app_name=NOME_APP, user_id=sessao.user_id, session_id=sessao.id
    )
    return {
        "resposta": "\n".join(t.strip() for t in textos if t.strip()),
        "confirmacoes_pendentes": confirmacoes_pendentes(atualizada.events),
    }


@api.post("/sessoes", status_code=201)
async def criar_sessao(corpo: NovaSessao) -> dict[str, str]:
    apartamento = corpo.apartamento.strip()
    if not banco.apartamento_existe(apartamento):
        raise HTTPException(status_code=422, detail="Apartamento inexistente.")
    # O apartamento entra no state uma única vez, aqui. É dele que as tools leem.
    sessao = await api.state.sessoes.create_session(
        app_name=NOME_APP, user_id=_usuario(apartamento), state={CHAVE_APARTAMENTO: apartamento}
    )
    await asyncio.to_thread(banco.registrar_sessao, sessao.id, apartamento)
    return {"session_id": sessao.id}


@api.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, corpo: Mensagem) -> dict[str, Any]:
    async with api.state.locks[session_id]:
        sessao = await _sessao(session_id)
        mensagem = types.Content(role="user", parts=[types.Part(text=corpo.texto)])
        return await _executar(sessao, mensagem)


@api.post("/sessoes/{session_id}/confirmacoes", response_model=None)
async def responder_confirmacao(session_id: str, corpo: RespostaConfirmacao) -> dict[str, Any] | JSONResponse:
    async with api.state.locks[session_id]:
        sessao = await _sessao(session_id)
        pendentes = {c["id"] for c in confirmacoes_pendentes(sessao.events)}
        if corpo.id not in pendentes:
            return JSONResponse(
                status_code=409,
                content={"detail": "Não existe confirmação pendente com esse id nesta sessão."},
            )
        # A resposta vai como FunctionResponse do adk_request_confirmation com o
        # mesmo id do pedido. O Runner acha a invocação pausada por esse id e
        # retoma no agente que chamou a tool, que roda de novo com
        # tool_context.tool_confirmation preenchido.
        resposta = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=corpo.id, name=CONFIRMACAO, response={"confirmed": corpo.confirmado}
                    )
                )
            ],
        )
        return await _executar(sessao, resposta)


@api.get("/sessoes/{session_id}/eventos")
async def listar_eventos(session_id: str) -> list[dict[str, Any]]:
    sessao = await _sessao(session_id)
    return [e.model_dump(mode="json", by_alias=True, exclude_none=True) for e in sessao.events]


@api.get("/apartamentos/{apartamento}/reservas")
async def reservas_do_apartamento(apartamento: str) -> list[dict[str, str]]:
    return await asyncio.to_thread(banco.reservas_do_apartamento, apartamento)


@api.get("/apartamentos/{apartamento}/visitantes")
async def visitantes_do_apartamento(apartamento: str) -> list[dict[str, str]]:
    return await asyncio.to_thread(banco.visitantes_do_apartamento, apartamento)
