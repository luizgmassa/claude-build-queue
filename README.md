# build-queue

Fila FIFO de máquina para execuções Gradle/JVM dos agentes Claude neste Mac, com monitor de memória,
limpeza de processos e página em http://127.0.0.1:8787.

## Regras

- **2 vagas**, contando o runner self-hosted do GitHub (`Runner.Worker` vivo = 1 vaga). O runner nunca
  espera: se chega com 2 builds locais rodando, fica 3 até um terminar, e ninguém novo entra.
- **FIFO estrito**: só o primeiro da fila pode entrar.
- **Memória**: pressão do macOS `warn` só deixa entrar com zero builds locais; `critical` não deixa ninguém.
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

- build de agente sem vaga: SIGTERM na árvore, SIGKILL após 10 s;
- processo de agente cuja sessão `claude` já morreu;
- daemon Gradle de agente ocioso há 20 s (sem cliente conectado) e sem concessão nos últimos 20 s;
- daemon Kotlin de agente ocioso há 20 s, só com zero builds locais rodando;
- worker Gradle cujo daemon pai morreu (ppid 1).

O dono de cada processo vem das variáveis `CLAUDE_PID`, `CLAUDE_CODE_SESSION_ID` e `RALPHY_STATUS_FILE`,
que todo processo criado por um agente herda, inclusive daemons soltos.

## Instalação

```
./install.sh                       # gq em ~/.local/bin, agente do launchd
python3 -m unittest tests/test_core.py
python3 tests/live_check.py        # fila real isolada na porta 8788
```

Gancho em `~/.claude/settings.json`, `hooks.PreToolUse`:

```json
{"matcher": "Bash", "hooks": [{"type": "command",
  "command": "/opt/homebrew/bin/python3 $HOME/Projects/claude-build-queue/bq/hook.py"}]}
```

Estado: `~/.local/share/build-queue/` (`queue.db`, `config.json`). Log: `~/Library/Logs/build-queue.log`.
