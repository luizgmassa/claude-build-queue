# Claude Build Queue

[English](README.md) · **Português**

Fila FIFO de máquina para os builds pesados dos agentes Claude (Gradle/JVM, Xcode, Node/JS, C/C++, .NET,
Swift, Rust, Go, Maven, sbt), com monitor de memória, limpeza de processos e página em http://127.0.0.1:8787.
Roda em macOS, Linux e Windows.

![Página da fila: vagas, memória, builds executando e aguardando, agentes](docs/queue.png)

![Dialog do agente: sessão, cada processo com sua parcela de memória, tickets](docs/agent-dialog.png)

A página fala inglês (EN-US) e português (PT-BR): abre no idioma do navegador e guarda a escolha feita no
seletor. Linhas de comando e IDs de sessão estão mascarados nestes prints.

## Regras

Os números abaixo são os padrões; cada um deles é uma configuração (veja [Configurações](#configurações)).

- **2 vagas**, contando o runner self-hosted do GitHub (`Runner.Worker` vivo = 1 vaga, a menos que o job dele tenha ticket: aí só o ticket conta, e
  conta 0 no Mac se o ticket estiver na máquina de build). O runner nunca
  espera: se chega com 2 builds locais rodando, fica 3 até um terminar, e ninguém novo entra.
- **FIFO estrito**: só o primeiro da fila pode entrar.
- **Sem teto fixo**: as 2 primeiras vagas entram livres. Cada build acima delas entra um por vez, e só se:
  a pressão de memória não está crítica; o swap não cresceu 0,5 GB ou mais nos últimos 60 s; passaram 30 s
  desde a última entrada extra; e a memória disponível do sistema, menos a reserva de pico dos builds em
  execução, menos a estimativa do item, deixa ≥ 6 GB de folga. A reserva de pico de um build em execução é
  a estimativa dele menos a memória que ele usa agora (nunca abaixo de 0): um build recém-iniciado ainda
  conta a memória que vai tomar. Um build que já rodou antes (mesmo repo, worktrees juntas, e mesmo build: os
  comandos de build que ele roda, sem wrappers como `perl -e 'alarm N; exec @ARGV'`, `timeout` ou `env`, flags
  de silêncio ou redirecionamentos; um script shell vale pelos builds escritos nele mais os seus argumentos; sem
  execução desse build, as últimas do mesmo repo e tipo de comando) reserva, no lugar da estimativa, a média do maior consumo que cada uma das suas 3 últimas execuções
  ainda teve daquele segundo em diante, menos o atual: passado o último pico, a reserva cai e a fila anda. Uma
  execução que já tinha terminado naquele segundo conta 0. Guardam a curva os tickets que saem com exit 0 e
  os builds sem ticket, desde que medidos desde o início. Reservam os builds da fila, o runner (pela estimativa de `gradle-check`) e
  os builds sem ticket (build de agente sem vaga, build fora de agente, pelo tipo de comando). Quando um
  sinal piora, nada é morto: as entradas extras param até ele passar. `gq config max N` limita as vagas a N
  (no mínimo o número de vagas base); `gq config max off`, o padrão, tira o limite.
- **Estimativa por tipo de comando** (`gradle-check`, `gradle-backend`, `gradle-ios`, `gradle-test`,
  `gradle-lint`, `gradle`, `xcodebuild`, `java`, `pod`, `uv`, `node`, `cc`, `bazel`, `dotnet`, `msbuild`,
  `swift`, `cargo`, `go`, `maven`, `sbt`): maior pico das últimas 5 execuções liberadas
  do mesmo tipo; antes disso, a estimativa inicial do tipo, nas configurações. O pico soma a árvore do `gq`, o daemon
  Gradle que atende o cliente, os workers dele e o daemon Kotlin ligado a ele.
- **Memória**: só a pressão crítica bloqueia (ninguém entra, nem por vaga extra). macOS: nível `critical` do
  kernel (`warn` não bloqueia). Linux: PSI `full avg10` ≥ 10% em `/proc/pressure/memory`. Windows: o aviso de
  memória baixa do kernel (`LowMemoryResourceNotification`). Sem esse sinal: menos de 5% da RAM disponível.
- Contam como execução: `./gradlew`, `xcodebuild`, `pod install`, `java`/`kotlinc` direto, `uv run`.
- Também passam pela fila (o gancho exige `gq run`), cada um com seu tipo de comando:
  - Node/JS: `npm`/`pnpm`/`yarn`/`bun` install, test e scripts com nome de build/test/compile/typecheck/e2e
    (não dev/watch/serve/start), `npx`/`exec`/`dlx` das ferramentas abaixo, `tsc`, `next build`, `vite build`,
    `jest`, `vitest` (fora do modo watch), tarefas de build/test do `nx`/`turbo`, `webpack` (não `serve`/watch);
    valores de `--filter`/`--workspace`/`--cwd` são entendidos (`pnpm --filter web build`);
  - C/C++: `cmake --build`, `make`, `ninja`, `msbuild`, `clang`/`gcc`/`g++`/`cc` direto com arquivo de entrada,
    `bazel build|test|coverage`;
  - .NET: `dotnet build|test|publish|restore|pack|msbuild`;
  - outros: `swift build|test`, `cargo build|test|bench|check|clippy`, `go build|test|install`, `mvn`/`mvnw`, `sbt`.

  Consultas de versão e ajuda (`--version`, `--help`, `/version`) e subcomandos baratos (`npm run dev`,
  `go vet`, `dotnet run`, `cmake -S . -B build`) ficam fora.
- Build sem vaga nunca é morto, Gradle incluído: o gancho é a única imposição. Ele ainda ocupa vaga, então a
  fila espera por ele.
- Builds fora de agente (seu terminal, Android Studio) contam vaga, mas nunca são mortos.
- Nunca tocados: runner (`GITHUB_ACTIONS`, ou linha de comando com um marcador do runner, `/actions-runner/`
  por padrão), processo cuja linha de comando contém um caminho protegido (nenhum por padrão; um backend local,
  por exemplo), processos `claude`, o próprio servidor.

## Configurações

O botão ⚙ da página abre as configurações. A parte simples tem as vagas base, o teto, um perfil de memória
(Cuidadoso, Equilibrado, Ousado: folga, limite de swap e intervalo entre builds extras juntos), a limpeza e o
início com o sistema. **Avançado** tem todas as configurações, cada uma com seu padrão: limites da fila e da
memória, tempos da limpeza, caminhos protegidos e marcadores do runner (um por linha), a estimativa inicial de
cada tipo de comando, a porta e o intervalo de varredura.

**Salvar e aplicar** valida cada valor (nada é gravado enquanto um estiver errado), grava `config.json` e
reinicia o servidor; os builds em andamento continuam e o `gq` reconecta. Uma porta nova leva a página para ela;
o `gq` e o gancho também leem a porta do `config.json`, mas um `gq` já rodando continua na antiga, então mude
sem build esperando. `BQ_PORT` no ambiente do servidor vale no lugar da configuração. O início com o sistema é a
chave do próprio gerenciador de serviços (launchd, `systemctl --user`, o gatilho de logon da tarefa do Windows);
reinstalar liga de novo. A página só aceita essas mudanças vindas dela mesma: requisição JSON cujo `Host` e
`Origin` são `127.0.0.1` ou `localhost` na porta do servidor.

## Uso pelos agentes

```
cd ../worktree && gq run -- ./gradlew :shared:check      # Bash com run_in_background: true
gq status
gq config max 6        # teto opcional de vagas; desligado por padrão
```

`gq` pega um ticket, espera a vaga sem gastar token, roda o comando e libera a vaga ao sair (inclusive em
crash: o servidor recolhe o ticket quando o PID do `gq` morre). `gq` dentro de um `gq` reaproveita a vaga.

O gancho `PreToolUse` global (`bq/hook.py`) nega todo Bash que rode esses comandos fora de `gq run`,
e todo `gq run` sem `run_in_background`.

`gq status` mostra as vagas, a memória, por que a fila está parada e os tickets abertos; `(ended)` marca o
ticket cujo agente morreu. `gq`, o gancho e o log do servidor falam inglês; só a página é traduzida.

## Builds remotas (`remote` no `config.json`)

Desligado por padrão. Com a chave `remote`, o servidor desta fila é o único agendador de dois lugares: esta máquina e
uma máquina de build. O Mac é o destino padrão. Quando o primeiro da fila não entra aqui (vagas base cheias e a vaga
extra recusada por memória, swap ou pelo máximo configurado, ou pressão de memória a partir de `block_pressure`, crítica por padrão), os tickets elegíveis
que esperam, em ordem, ocupam as vagas livres da máquina de build (`slots`). Recusa só pelo intervalo entre extras
(`extra_gap_s`) não conta: ninguém vai para lá por ela. O destino é gravado uma vez, na concessão, e não muda; um
ticket que não pode rodar remoto guarda o lugar para a próxima vaga local. A máquina de build precisa só de `ssh`,
`rsync` e do que a build lê do `local.properties` (o SDK do Android por `ANDROID_HOME`, por exemplo), porque arquivo
ignorado não é copiado; não precisa de `gq`. Edite o `config.json` à mão; a página não mostra essa chave e a
mantém no Salvar e aplicar.

```json
{"remote": {"host": "me@build-vm", "root": "~/remote-builds",
  "ssh_options": ["-i", "~/.ssh/google_compute_engine"],
  "instance": "build-vm", "zone": "us-central1-a", "project": "my-project"}}
```

- `host` (obrigatório): o alvo do `ssh`. `root` (obrigatório): onde ficam as cópias lá; `~/x` é relativo ao
  home remoto.
- `ssh_options` (opcional): argumentos a mais do `ssh`, usados também pelo `rsync`.
- `instance`, `zone`, `project` (opcionais, os três ou nenhum): `gcloud compute instances start` antes de cada
  build remota, para ligar uma VM parada do Compute Engine. Um `describe` confere em seguida que ela está subindo:
  um start enviado enquanto a VM ainda se desliga (por ociosidade) termina DONE sem ligá-la, então o `gq` espera o
  desligamento acabar e liga de novo, por até 180 s. Depois o `gq` espera até 180 s por `ssh … true`.
- `slots` (opcional, inteiro a partir de 1, padrão 2): quantas builds a máquina de build roda ao mesmo tempo. O
  servidor as conta; um ticket ocupa uma da concessão até o `release` (ou até o `gq` dele morrer).
- `cap_metadata_key` (opcional, exige `instance`, `zone` e `project`): antes de ligar a máquina, o `gq` lê os
  metadados dela (`gcloud compute instances describe`); o valor `true` nessa chave (uma função de orçamento a grava)
  torna a máquina indisponível (`budget exceeded`).
- `fetch_back` (opcional, lista de padrões do `rsync`): voltam da máquina de build além dos relatórios, resultados
  de teste e saídas (`build/reports`, `build/test-results`, `build/outputs`), por exemplo
  `build/generated/openapi/***`. Padrão: nenhum. Antes de cada comando a máquina de build apaga o que vai voltar
  (estes padrões e os três acima), para que relatório de execução anterior nunca chegue a esta máquina; o cache de
  build do Gradle restaura a saída das tarefas que não mudaram.
- `local_tasks` (opcional): tarefa que contém um destes, sem diferenciar caixa, roda local. Padrão:
  `ktlintFormat` (reescreve as fontes daqui), `ios`, `generateDummyFramework`, `installDebug`, `connected`,
  e `dependencyCheck`, `applyMigrations`, `assemble`, `bundle`: leem chave, banco ou arquivo de assinatura que
  só existem aqui (`~/.config`, o ambiente). O `gq` não manda variável de ambiente nem arquivo fora do checkout,
  então nenhum segredo sai desta máquina.

Elegível: `./gradlew …` ou `perl -e '<código>' ./gradlew …` (repassado inteiro, para o `alarm` valer lá), rodado
dentro de um checkout git, sem tarefa local e sem `-p`/`--project-dir` fora do checkout. Os valores de `-x`,
`--tests`, `-p`, `-I`, `-c` e `--console` não são tarefas. Todo o resto (`xcodebuild`, `pod`, `uv`, `node`, um
script, um `gq` aninhado) fica local.

O cliente só informa ao servidor, ao entrar na fila, se o comando é elegível. Quando o ticket é concedido para a
máquina de build, o `gq`:

1. liga a máquina e copia o checkout para `<root>/<repositório principal>/<worktree>` com `rsync --delete`, sem
   `/.git` e sem todo arquivo que o `.gitignore` ignora (`local.properties` incluído); `build/`, `.gradle/` e
   `.kotlin/` remotos sobrevivem, e a build remota continua incremental. O ticket fica concedido, na vaga da
   máquina de build, daqui até o fim;
2. imprime `[gq] remote <host>: <cmd>` e roda `ssh -tt <host> 'cd … && exec <cmd>'` (sem `gq` lá);
3. traz de volta `build/reports/`, `build/test-results/` e `build/outputs/` de todo projeto, mesmo quando a
   build falha, e sai com o código de saída remoto.

Se o orçamento acabou, a VM não liga, o `ssh` não responde ou a primeira cópia falha, o `gq` imprime
`[gq] remote unavailable (<passo>: <erro>)`, devolve o ticket à fila (mesmo id, portanto o mesmo lugar) e a máquina
de build sai da escolha por 300 s. A build não roda em lugar nenhum até a próxima concessão. Depois que a build
remota começou ela nunca roda local: conexão perdida sai com 255 e `[gq] remote connection lost`. SIGTERM, SIGINT,
SIGHUP ou SIGALRM no `gq` local derrubam o `ssh`; a pseudo-TTY faz o sshd mandar SIGHUP à build remota, e o
`release` do ticket libera a vaga da máquina de build. Cliente que não informa `remote` (um `gq` antigo) nunca
recebe a máquina de build. Chave inválida imprime `[gq] remote config ignored (<campo>)` e não muda nada. Não existe
no Windows (sem `flock`, sem `rsync` nativo).

### A visão da fila geral

Com a chave `remote`, `gq status` e a página listam cada ticket uma vez, Mac e máquina de build juntos. Cada ticket
tem um destino: `mac`, `vm` (`vm (starting)` enquanto a máquina liga) ou vazio enquanto espera. O `gq status` acrescenta
a linha `vm <usadas>/<vagas> in use  state=<estado>` depois da linha de vagas, e a página ganha um card e uma coluna
de destino. Sem a chave nada disso aparece e a saída não muda. Um ticket remoto fica na lista até o `release`.

O `state` é o primeiro que valer: `no_budget` (a metadata `cap_metadata_key` é `true`, ou a última partida falhou com
`budget exceeded`); `unavailable (<motivo>)` (falha recente, por 300 s); `starting` ou `on` (um ticket na máquina está
ligando ou rodando); senão o que o Compute Engine diz: `RUNNING` é `on`, `STAGING` e `PROVISIONING` são `starting`,
o resto é `off`, e `unknown` antes da primeira resposta ou quando o `gcloud` falha. Uma thread do servidor roda
`gcloud compute instances describe` a cada 60 s (o `tick` nunca espera por ela; procura no `PATH`,
`/opt/homebrew/bin` e `/usr/local/bin`). Sem `instance`, `zone` e `project` nada é consultado e a máquina ociosa conta
como `on`.

## Limpeza (`gq enforce on|off`)

Em `off` (simulação) a página só lista o que mataria. Em `on`:

- processo de agente cuja sessão `claude` já morreu: SIGTERM na árvore, SIGKILL após 10 s (o Windows não tem
  término gracioso: morre na hora);
- daemon Gradle de agente ocioso há 20 s (sem cliente conectado) e sem concessão nos últimos 20 s;
- daemon Kotlin de agente ocioso há 60 s, mesmo com builds rodando (o KGP cai no compilador dentro do Gradle);
- worker Gradle cujo daemon pai morreu (o pai deixou de ser um processo Gradle: init, subreaper do Linux ou
  PID reaproveitado no Windows);
- execução de toolchain (`npm`, `make`, `cargo`, ...) e servidor de build cujo agente morreu: nós reaproveitados
  do MSBuild (`MSBuild.dll /nodemode:`), `VBCSCompiler`, o servidor do Razor (`rzc.dll server`), o servidor do
  bazel, `tsc --watch`, `eslint_d`. Servidores de linguagem de IDE e caches (`tsserver`, `rust-analyzer`,
  `clangd`, `sourcekit-lsp`, `sccache`) nunca são tocados;
- compilador (`clang`, `gcc`, `cc1plus`, `swift-frontend`, `rustc`) cujo driver morreu (init ou um subreaper o
  adotou). Compilador com pai vivo (IDE, `pip install`) fica em paz.

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
