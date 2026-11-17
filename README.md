# Residencial Aurora: assistente virtual com Google ADK

Assistente do aplicativo dos moradores do Residencial Aurora, construído com Google ADK 2.9.1 e exposto por uma API FastAPI em `http://localhost:8000`. Pelo chat o morador reserva e cancela áreas comuns, autoriza visitantes e tira dúvidas sobre o regulamento.

A ideia que guia o projeto é a do enunciado: o modelo decide o caminho da conversa, e o código decide o que é permitido. Toda regra crítica fica em Python ou no SQLite, e o prompt só orienta o tom e a delegação.

```
aurora/
  agentes.py       agente principal, especialistas e App do ADK
  ferramentas.py   tools de reservas, visitantes e regulamento
  banco.py         dados do condomínio em SQLite (esquema, restauração, gravações)
  regulamento.py   divisão do regulamento por capítulo
  api.py           rotas HTTP, Runner e serviço de sessão
  restaurar.py     comando de restauração
  __main__.py      comando de subida
dados/             estado inicial do condomínio (nunca é alterado)
var/               bancos SQLite gerados em tempo de execução (fora do Git)
```

## Arquitetura

O morador sempre conversa com o agente principal, `assistente_aurora`. Ele não tem nenhuma tool de reserva ou visitante: o trabalho dele é entender o pedido e entregar ao especialista certo. São três especialistas, e cada um é acionado de um jeito por um motivo diferente.

| Agente | Responsabilidade | Como é acionado | Tools |
|---|---|---|---|
| `assistente_aurora` | Recebe o morador e distribui o trabalho | Agente raiz do `App` | `especialista_regulamento` (AgentTool) |
| `especialista_reservas` | Consultar, criar e cancelar reservas | Sub-agente, por `transfer_to_agent` | `listar_minhas_reservas`, `verificar_disponibilidade`, `reservar_area`, `cancelar_reserva` |
| `especialista_visitantes` | Consultar e autorizar visitantes | Sub-agente, por `transfer_to_agent` | `listar_meus_visitantes`, `autorizar_visitante` |
| `especialista_regulamento` | Responder dúvidas sobre o regulamento | `AgentTool` do agente principal | `listar_capitulos`, `ler_capitulo` |

Reservas e visitantes são sub-agentes por transferência porque as chamadas de tool deles precisam ficar gravadas na sessão do morador. É ali que nasce o pedido de confirmação, e é para o mesmo agente que a resposta precisa voltar quando o morador aprova. Uma transferência mantém tudo na mesma sessão persistida, então o Runner consegue retomar a invocação pausada no especialista que pediu a confirmação, inclusive depois de reiniciar a API. Os dois especialistas têm `disallow_transfer_to_peers=True`: quando o assunto muda, eles devolvem a conversa ao agente principal, que continua sendo o único ponto de roteamento.

O especialista de regulamento é um `AgentTool` pelo motivo oposto. O `AgentTool` executa o agente num Runner próprio com `InMemorySessionService`, numa sessão descartável. Os capítulos que ele lê ficam nessa sessão interna, e na sessão do morador entram só a chamada da tool e a resposta final. Como é uma consulta pontual, ele também não fica ativo depois da resposta, então as mensagens seguintes não arrastam o contexto dele.

O `App` é criado com `ResumabilityConfig(is_resumable=True)`. Com isso, quando uma tool pede confirmação, a invocação pausa ali mesmo, e a resposta do morador retoma essa mesma invocação no agente que fez a chamada.

O modelo de cada agente vem do `.env` (`MODELO_PRINCIPAL` e `MODELO_ESPECIALISTAS`), com `gemini-3.5-flash` como padrão. Todos usam retry com backoff exponencial para erros temporários da API, como 429 e 5xx.

### Armazenamento

Tudo fica em SQLite, sem serviço externo. `var/aurora.db` guarda reservas, visitantes, os códigos de reserva já emitidos e o vínculo entre sessão e apartamento. `var/sessoes.db` é o banco do `SqliteSessionService` do ADK, com as sessões e todos os eventos. Os dois bancos usam WAL. Os arquivos de `dados/` só são lidos, para montar o estado inicial.

## Garantias

### Garantia 1: cobrança ou acesso só com confirmação

Em `aurora/ferramentas.py`, `reservar_area` e `autorizar_visitante` só gravam quando o ADK chama a tool de novo com `tool_context.tool_confirmation` preenchido. Na primeira chamada, elas registram o pedido e retornam sem gravar nada:

```python
confirmacao = tool_context.tool_confirmation
if confirmacao is None:
    ...
    if a["taxa"] > 0:
        tool_context.request_confirmation(
            hint=f"Reservar {a['nome']} em {d} gera cobrança de R$ {a['taxa']:.2f}.",
            payload={"area": a["id"], "data": d, "taxa": a["taxa"]},
        )
        return {"status": "aguardando_confirmacao", ...}
elif not confirmacao.confirmed:
    return {"status": "recusada", "mensagem": "O morador recusou a cobrança. Nada foi reservado."}
```

A quadra tem taxa zero, então `reservar_area` grava direto e não pede confirmação. `cancelar_reserva` também não pede. `autorizar_visitante` pede sempre, porque liberar entrada é sempre uma ação de acesso.

O `tool_confirmation` não tem como vir da conversa. O ADK só o preenche quando chega um `FunctionResponse` de `adk_request_confirmation` com o id do pedido, e quem monta esse `FunctionResponse` é a rota `POST /sessoes/{id}/confirmacoes`, em `responder_confirmacao` (`aurora/api.py`). Uma mensagem como "já estou confirmando aqui" é texto do morador e não toca nesse mecanismo.

A rota só aceita ids que estão pendentes naquela sessão. `confirmacoes_pendentes` (`aurora/api.py`) lê os eventos gravados e devolve os pedidos de `adk_request_confirmation` que ainda não têm resposta:

```python
pendentes = {c["id"] for c in confirmacoes_pendentes(sessao.events)}
if corpo.id not in pendentes:
    return JSONResponse(status_code=409, content={...})
```

Uma confirmação já respondida tem o seu `FunctionResponse` gravado como evento e sai da lista, então reenviar o mesmo id recebe 409 e nada é executado. Como a lista vem dos eventos persistidos, ela vale também depois de reiniciar a API. Um lock por sessão em `api.state.locks` serializa as requisições de uma mesma sessão.

### Garantia 2: cada sessão pertence a um apartamento

O apartamento entra no state da sessão uma única vez, em `criar_sessao` (`aurora/api.py`):

```python
sessao = await api.state.sessoes.create_session(
    app_name=NOME_APP, user_id=_usuario(apartamento), state={CHAVE_APARTAMENTO: apartamento}
)
```

Nenhuma tool recebe apartamento como parâmetro, então o modelo não tem onde escolher um. Todas leem o apartamento com `apartamento_da_sessao` (`aurora/ferramentas.py`), e nenhuma tool escreve no state:

```python
def apartamento_da_sessao(tool_context: ToolContext) -> str:
    apartamento = tool_context.state.get(CHAVE_APARTAMENTO)
    if not apartamento:
        raise SessaoSemApartamento("sessão sem apartamento")
    return str(apartamento)
```

As consultas em `aurora/banco.py` filtram por esse apartamento (`reservas_do_apartamento`, `visitantes_do_apartamento`, `cancelar_reserva` com `WHERE apartamento = ?`). Um pedido para cancelar a reserva de outro apartamento procura só entre as reservas da sessão e volta como `nao_encontrada`. Conferir a agenda passa por `dono_da_data`, mas `verificar_disponibilidade` e `reservar_area` só devolvem ao modelo `livre`, `ocupada` ou `ocupada por uma reserva sua`, sem código nem número de apartamento. Como o dado de outro apartamento nunca sai de uma tool, ele não chega aos eventos da sessão, seja qual for a mensagem do morador.

### Garantia 3: nada se perde no reinício

As sessões e os eventos ficam no `SqliteSessionService` do ADK, criado em `lifespan` (`aurora/api.py`) sobre `var/sessoes.db`:

```python
servico = SqliteSessionService(str(ARQUIVO_SESSOES))
app.state.runner = Runner(app=criar_app(), session_service=servico)
```

Reservas e visitantes ficam em `var/aurora.db`. Na subida, `banco.inicializar()` cria o esquema e só carrega os arquivos de `dados/` se o banco estiver vazio, então reiniciar a API não apaga nada. Uma confirmação pendente também sobrevive ao reinício: o pedido está nos eventos, e o `App` resumable retoma a invocação a partir deles. Os códigos de reserva são gerados em `_novo_codigo` (`aurora/banco.py`) e gravados em `codigos_emitidos`, uma tabela com chave primária que a restauração não limpa. Por isso um código nunca se repete, nem o de uma reserva cancelada.

### Garantia 4: o regulamento é consultado, não carregado

O agente principal não tem o regulamento nas instruções. As instruções dele, em `criar_agente_principal` (`aurora/agentes.py`), só explicam a quem delegar. O texto só é lido pelo `especialista_regulamento`, em pedaços, por duas tools de `aurora/ferramentas.py`: `listar_capitulos` devolve só números e títulos, e `ler_capitulo` devolve um capítulo por vez. A divisão por capítulo está em `aurora/regulamento.py`.

O especialista roda como `AgentTool`:

```python
# O especialista de regulamento roda como AgentTool: o ADK executa ele
# numa sessão própria em memória, então os capítulos que ele lê não entram
# nos eventos da sessão do morador. Só a resposta final volta (Garantia 4).
...
tools=[AgentTool(agent=regulamento)],
```

Isso não depende do modelo se comportar bem. Mesmo que o especialista leia um capítulo errado antes de achar o certo, essa leitura acontece no Runner interno do `AgentTool` (`google/adk/tools/agent_tool.py`, que cria `InMemorySessionService()`) e não é gravada na sessão do morador.

### Garantia 5: dois moradores, uma reserva

A exclusividade é do SQLite, no instante do INSERT. Em `aurora/banco.py`:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS reserva_ativa_unica
    ON reservas (area, data) WHERE status = 'ativa';
```

`criar_reserva` grava dentro de uma transação `BEGIN IMMEDIATE`. Quando duas aprovações chegam juntas, o SQLite serializa as duas escritas. A segunda bate no índice único, e o `sqlite3.IntegrityError` vira `None`:

```python
try:
    with transacao() as con:
        codigo = _novo_codigo(con)
        con.execute("INSERT INTO reservas (...) VALUES (?, ?, ?, ?, ?)", (...))
        return codigo
except sqlite3.IntegrityError:
    return None
```

`reservar_area` transforma esse `None` em `{"status": "indisponivel", ...}`, uma resposta normal da tool, e a rota devolve 200 nas duas aprovações. A conferência de agenda feita antes de pedir a confirmação serve só para não pedir aprovação de uma data que já está ocupada. Quem garante a exclusividade é o índice. Cancelar muda o status para `cancelada`, e a reserva cancelada sai do índice parcial, o que libera a data.

## Como rodar

Pré-requisitos: Python 3.12 ou mais novo, [uv](https://docs.astral.sh/uv/) e uma chave do [Google AI Studio](https://aistudio.google.com/apikey). Não há serviço externo para subir: os dois bancos são arquivos SQLite em `var/`.

1. Configure o ambiente:

   ```bash
   cp .env.example .env
   ```

   | Variável | Obrigatória | Uso |
   |---|---|---|
   | `GOOGLE_API_KEY` | sim | Chave do Google AI Studio |
   | `GOOGLE_GENAI_USE_VERTEXAI` | sim, `FALSE` | Usa a Gemini API do AI Studio, e não a Vertex AI |
   | `MODELO_PRINCIPAL` | não | Modelo do agente principal. Padrão: `gemini-3.5-flash` |
   | `MODELO_ESPECIALISTAS` | não | Modelo dos especialistas. Padrão: `gemini-3.5-flash` |

2. Instale as dependências:

   ```bash
   uv sync
   ```

3. Restaure os dados iniciais, com a API parada. O comando volta reservas e visitantes ao estado de `dados/` e apaga as sessões:

   ```bash
   uv run python -m aurora.restaurar
   ```

4. Suba a API em `http://localhost:8000`:

   ```bash
   uv run python -m aurora
   ```

   Ctrl+C para a API. Subir de novo com o mesmo comando mantém sessões, reservas e visitantes.

Um teste rápido:

```bash
curl -s localhost:8000/apartamentos/101/reservas
S=$(curl -s -X POST localhost:8000/sessoes -H 'Content-Type: application/json' -d '{"apartamento": "101"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')
curl -s -X POST localhost:8000/sessoes/$S/mensagens -H 'Content-Type: application/json' -d '{"texto": "Reserve o salão de festas para 2030-04-20."}'
```
