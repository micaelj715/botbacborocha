# Bac Bo Monitor Pro

Monitor ao vivo (TipMiner), estatísticas, laboratório de backtest e alertas no Telegram.
Não prevê resultados e não aposta sozinho: cada rodada é independente.

## Iniciar (Windows)
Dois cliques em `iniciar.bat`. Abre em http://127.0.0.1:5000

Ou manualmente:
```
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

## Endpoints usados (fixos)
- History: `.../rounds/cc71e81d-8b56-4868-91c7-7224be543dce/history?limit=200&timezone=Atlantic/Cape_Verde`
- Live: `.../rounds/cc71e81d-8b56-4868-91c7-7224be543dce/live`

## Telegram
Copia `config.example.json` para `config.json` e preenche `telegram_token` e `telegram_chat_id`.
- `telegram_all_rounds: true` envia cada rodada; `false` envia só quando a sequência chega a `streak_alert`.

## Se não aparecerem rodadas
Abre a aba **Inspetor de dados** no painel. Ela mostra o JSON bruto que a API devolve, o código HTTP
de History e Live e quantos eventos foram ignorados. Copia um bloco de JSON de lá e o parser ajusta-se ao formato.

## Teste
`python test_parser.py` valida o parser com vários formatos de JSON.

## Estratégias, sentimento e horários
- **Estratégias:** 9 regras (contra/seguir sequência, alternância, maioria das últimas 10, Tie atrasado) são avaliadas
  em todo o histórico e em tempo real: entradas, green, red, % de acerto, saldo e comparação com o acaso (50% para
  Player/Banker; 11,3% para o Tie). A coluna "1ª / 2ª metade" mostra se o resultado se mantém ao longo do tempo.
- **Sentimento:** leitura descritiva das últimas 30 rodadas (lado dominante, ritmo, Tie atrasado).
- **Melhores horários:** % de green por hora (horário de Cabo Verde) para a estratégia escolhida.
- **config.json:** `tie_payout` (pagamento do Tie, padrão 5) e `tie_pushes_pb` (Player/Banker devolvidos no empate).
  Confirma os dois valores nas regras da mesa que estás a acompanhar.

## v3: sinais, gale, Telegram e configurações
- **Motor de sinais no servidor** (`signals.json` guarda o placar): os sinais e o Telegram funcionam com o navegador fechado.
- **Empate = green** nas entradas Player/Banker (`tie_as_green`).
- **Gale até G2** só quando o histórico do padrão mostra poucas perdas seguidas (`max_gale`: 0, 1 ou 2).
- **Configurações:** http://127.0.0.1:5000/settings (token, chat id, sinais, gale, cards visíveis).

## v4: motor adaptativo
- Os gatilhos de sequência são exatos (2, 3, 4, 5+ iguais), e cada padrão é avaliado só pela sua **forma recente** (memória que cai pela metade a cada 20 entradas).
- Padrão sem forma, em queda (3 reds seguidos) ou contradito por outro mais forte não gera sinal. Se a mesa muda de comportamento (taxa de repetição), a exigência sobe.
- O painel "Motor adaptativo" mostra a mesa, o estado de cada padrão e uma **validação só com o passado**: se ficar "dentro do acaso", não há vantagem real.

## v5
- Splash com dados girando até o histórico (600 rodadas, `history_limit`) e o ao vivo estarem prontos.
- Telegram limpo: só "Possível entrada" (com proteção de empate) e depois GREEN/RED com estatísticas. `telegram_all_rounds` (padrão false) reativa o envio de todas as rodadas. `tie_emoji` muda a bolinha do empate (ex.: "🟠").


## Histórico (v6)
A cada arranque o app recomeça limpo e carrega as **200 rodadas mais recentes** da API (`history_limit`, máx. 200),
sem buracos entre o que estava guardado e o que a API devolve agora. Depois acumula ao vivo. Para manter o banco entre
arranques, ponha `"reset_on_start": false` no `config.json` (só recomendado se o programa nunca ficar desligado).

## Celular (v6)
Ao iniciar, o app cria um link público HTTPS (`https://…trycloudflare.com`, sem IP) através do Cloudflare Tunnel.
Na primeira vez baixa o `cloudflared.exe` para a pasta do app. O link aparece no terminal e no botão 📱 do painel (com QR).
- No celular abre uma versão mobile própria (sinal de entrada, resultado, placar e mesa).
- O link só funciona com o programa a correr e **muda a cada arranque**.
- Segurança: o link inclui uma chave; sem ela o acesso é recusado. Pelo link só se vê o painel: definições, exportação e
  outros comandos ficam bloqueados. Não partilhe o link.
- Desligar: `"tunnel": false` no `config.json`.

## Meus padrões (v7): construtor por bolinhas
Abra `/builder` (botão 🧩 no painel, ou "＋ Criar / editar" no celular).
1. Toque nas bolinhas 🔵 Player, 🔴 Banker, 🟡 Tie ou ⚪ Qualquer para montar a sequência (da mais antiga para a mais recente). Toque numa bolinha da sequência para trocar a cor.
2. Escolha onde entrar: Player, Banker, Tie, "mesmo do último" ou "oposto do último".
3. Escolha o gale (0, 1 ou 2) e, se quiser, dê um nome.
O teste no histórico aparece na hora (entradas, green, red, acerto e comparação com o acaso).
Os padrões ficam em `custom.json` (máx. 20) e são vigiados ao vivo: o painel PC mostra um aviso, o celular destaca o cartão
com som e vibração, e o Telegram (se configurado) recebe a entrada e o resultado. "Empate = green" e "Player/Banker devolvidos"
seguem as opções do `config.json` (`tie_as_green`, `tie_pushes_pb`).

## Filtros e escala 20×6 (v8)
No construtor, o passo **4 · Filtros e proteções** define quando um padrão NÃO deve entrar:
- **Tendência:** "Não contra" bloqueia entradas contra o lado dominante das últimas 10/20/30 rodadas (com força mínima de 55/60/65%); "Só a favor" exige tendência e entrada nela.
- **Ritmo da mesa:** só entra com a mesa em sequências ou alternando.
- **Pausar após reds seguidos** (2 a 5), por 5 a 30 rodadas.
- **Intervalo entre entradas** (1 a 10 rodadas).
- **Horário** (hora de Cabo Verde).
"Aplicar recomendado" liga: não contra a tendência (20 rodadas, 55%) e pausa de 10 rodadas após 3 reds.
O teste mostra quantas ocorrências os filtros bloquearam. Cada padrão guarda os seus filtros em `custom.json`.

A **Escala 20×6** mostra as últimas 120 rodadas (6 linhas, coluna a coluna) com a tendência e o ritmo atuais, e um
**ranking** dos padrões só nessas 120 rodadas (com correção para poucas entradas). Tocar num padrão marca na escala onde
ele disparou (anel roxo = o padrão, amarelo = gale, verde = green, vermelho = red).

## Card de Entrada e pré-aviso "a analisar padrão" (v9)
- O painel do PC (`/`) tem agora um **card fixo "Entrada"** no topo, sempre visível (não só um popup): mostra "Monitorando
  padrões" quando não há nada a acontecer, **"Analisando padrão"** (cor âmbar) quando falta 1 rodada para um gatilho
  confirmar, e **"Sinal de entrada"** (cor do lado) em destaque quando há uma entrada ativa — tanto dos padrões fixos do
  motor adaptativo como dos "Meus padrões". Lista também, por baixo, cada padrão personalizado ativo ou em formação.
  O celular já tinha um card assim (o "hero" no topo); agora também mostra o estado "Analisando padrão".
  Se este card tiver sido escondido em **Configurações → Cards da interface**, ligue-o outra vez lá ("Entrada (destaque)").
- **Pré-aviso no Telegram:** quando falta exatamente 1 rodada para um padrão (seu ou fixo, se ligado) completar o gatilho,
  o servidor manda uma mensagem "🔎 Analisando padrão" antes da confirmação — dá tempo de se preparar. Liga/desliga em
  **Configurações → Telegram → "Avisar 'a analisar padrão'"** (`telegram_pre_alert` no `config.json`, ligado por padrão).
  Continua a precisar de `telegram_signals` ligado e do token/chat id configurados.
- **Só os "Meus padrões" geram entrada, por padrão:** os 9 padrões fixos do sistema (motor adaptativo) ficam só como
  estatística de referência — não abrem entrada, não mandam pré-aviso e não mandam Telegram, a não ser que você ligue
  **Configurações → Sinais e gale → "Sinais dos padrões fixos do sistema"** (`engine_signals` no `config.json`, desligado
  por padrão). Enquanto estiver desligado, o placar do cabeçalho (green/red/acerto do motor) fica escondido, porque é só
  dele; o placar dos "Meus padrões" continua a aparecer normalmente no card de cada um.

## Link fixo do celular
O endereço `trycloudflare.com` é um Quick Tunnel e muda quando o processo é reiniciado. Para manter o mesmo endereço, configure um Cloudflare Tunnel nomeado:
- `tunnel_url`: URL pública fixa do túnel (ex.: `https://painel.seudominio.com`)
- `cloudflare_tunnel_token`: token do túnel nomeado

Esses dois campos também podem ser preenchidos em **Configurações → Link do celular**. A chave `?k=...` agora é persistida em `access_key.txt`, portanto também não muda entre reinícios.
