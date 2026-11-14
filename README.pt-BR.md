# build-queue

[English](README.md) · **Português**

Fila FIFO de máquina para execuções Gradle/JVM dos agentes Claude, com monitor de memória, limpeza de
processos e página em http://127.0.0.1:8787. Roda em macOS, Linux e Windows.

![Página da fila: vagas, memória, builds executando e aguardando, agentes](docs/queue.png)

![Dialog do agente: sessão, cada processo com sua parcela de memória, tickets](docs/agent-dialog.png)

A página fala inglês (EN-US) e português (PT-BR): abre no idioma do navegador e guarda a escolha feita no
seletor. Linhas de comando e IDs de sessão estão mascarados nestes prints.

## Regras

- **2 vagas**, contando o runner self-hosted do GitHub (`Runner.Worker` vivo = 1 vaga). O runner nunca
  espera: se chega com 2 builds locais rodando, fica 3 até um terminar, e ninguém novo entra.
- **FIFO estrito**: só o primeiro da fila pode entrar.
- **Limite dinâmico, de 2 vagas até um teto dado pela RAM da máquina**: RAM ÷ 6 GB, arredondado para cima
  (16 GB → 3, 24 GB → 4, 32 GB → 6, 48 GB → 8; nunca abaixo de 2). Com a fila cheia, o limite sobe 1 vaga para o primeiro da fila só se:
  a pressão não está crítica, passaram 30 s desde a última subida, e a memória disponível do sistema menos a
  estimativa do item deixa ≥ 6 GB de folga. Cada execução que termina baixa o limite em 1 (nunca abaixo de 2);
  a próxima subida passa pela mesma análise. Cada subida soma exatamente 1 vaga: se quem não espera (runner,
  build fora de agente) já ocupa essa vaga, ninguém entra até a próxima subida.
- **Estimativa por tipo de comando** (`gradle-check`, `gradle-backend`, `gradle-ios`, `gradle-test`,
  `gradle-lint`, `gradle`, `xcodebuild`, `java`, `pod`, `uv`): maior pico das últimas 5 execuções liberadas
  do mesmo tipo; antes disso, a tabela `DEFAULT_GB` de `bq/core.py`. O pico soma a árvore do `gq`, o daemon
  Gradle que atende o cliente, os workers dele e o daemon Kotlin ligado a ele.
- **Memória**: só a pressão crítica bloqueia (ninguém entra, nem por vaga extra). macOS: nível `critical` do
  kernel (`warn` não bloqueia). Linux: PSI `full avg10` ≥ 10% em `/proc/pressure/memory`. Windows: o aviso de
  memória baixa do kernel (`LowMemoryResourceNotification`). Sem esse sinal: menos de 5% da RAM disponível.
- Contam como execução: `./gradlew`, `xcodebuild`, `pod install`, `java`/`kotlinc` direto, `uv run`.
- Builds fora de agente (seu terminal, Android Studio) contam vaga, mas nunca são mortos.
- Nunca tocados: runner (`GITHUB_ACTIONS`, `~/.gradle-health-hub-ci`), backend do staging
  (`~/.health-hub-local`), processos `claude`, o próprio servidor.

## Uso pelos agentes

```
cd ../worktree && gq run -- ./gradlew :shared:check      # Bash com run_in_background: true
gq status
```

`gq` pega um ticket, espera a vaga sem gastar token, roda o comando e libera a vaga ao sair (inclusive em
crash: o servidor recolhe o ticket quando o PID do `gq` morre). `gq` dentro de um `gq` reaproveita a vaga.

O gancho `PreToolUse` global (`bq/hook.py`) nega todo Bash que rode esses comandos fora de `gq run`,
e todo `gq run` sem `run_in_background`.

## Limpeza (`gq enforce on|off`)

Em `off` (simulação) a página só lista o que mataria. Em `on`:

- build de agente sem vaga: SIGTERM na árvore, SIGKILL após 10 s (o Windows não tem término gracioso: morre
  na hora);
- processo de agente cuja sessão `claude` já morreu;
- daemon Gradle de agente ocioso há 20 s (sem cliente conectado) e sem concessão nos últimos 20 s;
- daemon Kotlin de agente ocioso há 60 s, mesmo com builds rodando (o KGP cai no compilador dentro do Gradle);
- worker Gradle cujo daemon pai morreu (o pai deixou de ser um processo Gradle: init, subreaper do Linux ou
  PID reaproveitado no Windows).

O dono de cada processo vem das variáveis `CLAUDE_PID`, `CLAUDE_CODE_SESSION_ID` e `RALPHY_STATUS_FILE`,
que todo processo criado por um agente herda, inclusive daemons soltos.

## Instalação

Requer Python 3.10+. O servidor roda num venv com [psutil](https://github.com/giampaolo/psutil), que lê
processos, ambiente, conexões e memória nos três sistemas; `gq` e o gancho só usam a biblioteca padrão.

| Sistema | Instalar | Serviço `com.massa.build-queue` | Log |
|---|---|---|---|
| macOS | `./install.sh` | agente do launchd | `~/Library/Logs/build-queue.log` |
| Linux | `./install.sh` | `systemctl --user` | `journalctl --user -u com.massa.build-queue` |
| Windows | `powershell -ExecutionPolicy Bypass -File install.ps1` | tarefa do Agendador, no logon | `~/.local/share/build-queue/server.log` |

No Linux o serviço de usuário para no logout; para mantê-lo vivo: `loginctl enable-linger $USER`.
No Windows o instalador põe `gq` (para o Git Bash, onde o Claude Code roda o Bash) e `gq.cmd` em
`~/.local/bin`; essa pasta precisa estar no PATH.

Testes, os mesmos que o GitHub Actions roda em macOS, Linux e Windows (no Windows o Python do venv fica em
`Scripts\python.exe`):

```
~/.local/share/build-queue/venv/bin/python -m unittest discover -s tests -p "test_*.py"
~/.local/share/build-queue/venv/bin/python tests/live_check.py   # fila real isolada na porta 8788
```

Gancho em `~/.claude/settings.json`, `hooks.PreToolUse`:

```json
{"matcher": "Bash", "hooks": [{"type": "command",
  "command": "python3 $HOME/Projects/claude-build-queue/bq/hook.py"}]}
```

No Windows, `python` no lugar de `python3`. Estado: `~/.local/share/build-queue/` (`queue.db`, `config.json`).
