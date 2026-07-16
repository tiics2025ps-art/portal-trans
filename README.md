# Coletor de Documentos Públicos

Projeto Python para descobrir, baixar lentamente, validar e armazenar documentos públicos em uma pasta restrita do Google Drive, usando GitHub Actions como executor temporário.

O coletor foi projetado para ser conservador. Ele não alterna IP, não usa proxy, Tor ou VPN, não cria múltiplos runners e não tenta contornar bloqueios. HTTP 403 interrompe a execução e exige liberação manual. HTTP 429 interrompe a execução e registra o prazo informado em `Retry-After`.

## Arquitetura

```text
GitHub Repository
    ↓
GitHub Actions
    ↓
Coletor Python
    ↓
Descoberta e fila persistente
    ↓
Download controlado
    ↓
Validação do PDF
    ↓
Google Drive
```

O runner do GitHub é descartável. O estado relevante fica no Google Drive:

```text
Documentos Públicos/
  Contratos/
  Empenhos/
  Processos/
  Logs/
  Estado/
    collector-state.sqlite3
    collector-lock.json
    daily-budget.json
    collector-state-*.bak
```

## Comportamento de segurança

- Uma requisição por vez.
- Intervalo aleatório entre 25 e 45 segundos.
- Pausa entre 5 e 10 minutos após cada 10 downloads.
- Limite inicial de 40 arquivos por execução.
- Limite compartilhado de 200 downloads por domínio por dia.
- Limite de 25 páginas de descoberta por execução.
- Fila limitada a 500 itens pendentes.
- Timeouts de conexão e leitura.
- Limite máximo de tamanho por arquivo.
- Máximo de redirecionamentos.
- URL final obrigatoriamente no mesmo domínio permitido.
- Respeito a `robots.txt`.
- Parada em CAPTCHA, login, bloqueio ou página HTML disfarçada de PDF.
- Nenhuma fonte real habilitada por padrão.
- Primeira execução obrigatoriamente em `DRY_RUN`.

## Estrutura do repositório

```text
.github/workflows/collector.yml
config/sources.example.yml
config/sources.yml
src/collector/
tests/
docs/example.log
requirements.txt
.env.example
.gitignore
README.md
```

## 1. Criar o repositório no GitHub

1. Crie um repositório privado, por exemplo `public-document-collector`.
2. Envie todo o conteúdo deste projeto para a branch `main`.
3. Abra a guia **Actions** e confirme que o workflow `Public document collector` aparece.
4. Não habilite o agendamento ainda.

Exemplo pela linha de comando:

```bash
git init
git add .
git commit -m "Adicionar coletor conservador de documentos públicos"
git branch -M main
git remote add origin https://github.com/SEU-USUARIO/public-document-collector.git
git push -u origin main
```

## 2. Criar o projeto no Google Cloud

1. Entre no Google Cloud Console.
2. Crie ou selecione um projeto dedicado ao coletor.
3. Acesse **APIs e serviços > Biblioteca**.
4. Procure por **Google Drive API**.
5. Clique em **Ativar**.

Use um projeto separado para facilitar auditoria e revogação. Misturar tudo na mesma conta porque “é mais fácil” costuma ser o prólogo de relatórios desagradáveis.

## 3. Criar a conta de serviço

1. Acesse **IAM e administrador > Contas de serviço**.
2. Crie uma conta, por exemplo `public-document-collector`.
3. Não conceda funções amplas no projeto se elas não forem necessárias.
4. Abra a conta criada.
5. Acesse **Chaves > Adicionar chave > Criar nova chave**.
6. Escolha JSON.
7. Guarde o arquivo em local seguro.

O conteúdo inteiro desse JSON será armazenado no GitHub Secret `GOOGLE_SERVICE_ACCOUNT_JSON`.

Nunca envie o JSON para o repositório. Nunca cole o conteúdo em issue, log, comentário ou arquivo de configuração versionado.

## 4. Criar e compartilhar somente a pasta necessária no Drive

1. No Google Drive, crie uma pasta chamada `Documentos Públicos`.
2. Copie o e-mail da conta de serviço, algo como:

```text
public-document-collector@seu-projeto.iam.gserviceaccount.com
```

3. Compartilhe somente a pasta `Documentos Públicos` com essa conta.
4. Conceda permissão de editor nessa pasta.
5. Não compartilhe a raiz inteira do Drive.
6. Copie o ID da pasta a partir da URL.

O coletor criará automaticamente as subpastas `Contratos`, `Empenhos`, `Processos`, `Logs` e `Estado`.

A API utiliza o escopo técnico do Drive, mas a conta de serviço somente enxerga os recursos efetivamente compartilhados com ela. A limitação prática deve ser feita pelo compartilhamento da pasta dedicada.

## 5. Adicionar os GitHub Secrets

No repositório, acesse:

```text
Settings
→ Secrets and variables
→ Actions
→ New repository secret
```

Crie:

```text
GOOGLE_SERVICE_ACCOUNT_JSON
GOOGLE_DRIVE_FOLDER_ID
```

Opcionalmente:

```text
COLLECTOR_CONTACT_EMAIL
```

Valores:

- `GOOGLE_SERVICE_ACCOUNT_JSON`: conteúdo completo do arquivo JSON da conta de serviço.
- `GOOGLE_DRIVE_FOLDER_ID`: ID da pasta `Documentos Públicos`.
- `COLLECTOR_CONTACT_EMAIL`: e-mail administrativo incluído no User-Agent.

O User-Agent usado é:

```text
ColetorDocumentosPublicos/1.0
```

Com contato configurado:

```text
ColetorDocumentosPublicos/1.0 (+mailto:administrador@exemplo.gov.br)
```

O coletor não imita navegador humano e não rotaciona User-Agent.

## 6. Configurar uma fonte

Edite `config/sources.yml`.

Exemplo desativado:

```yaml
sources:
  - name: portal_exemplo
    base_url: https://exemplo.gov.br
    enabled: false
    start_urls:
      - https://exemplo.gov.br/documentos
    allowed_path_prefixes:
      - /documentos
    document_types:
      - contratos
      - empenhos
    document_url_patterns:
      - '(?i)\.pdf(?:$|[?#])'
    follow_url_patterns:
      - '(?i)/documentos/'
```

Campos:

- `name`: identificador estável da fonte.
- `base_url`: domínio autorizado.
- `enabled`: precisa ser alterado explicitamente para `true`.
- `start_urls`: páginas iniciais de descoberta.
- `allowed_path_prefixes`: caminhos permitidos dentro do domínio.
- `document_types`: classificação usada para escolher a subpasta do Drive.
- `document_url_patterns`: expressões regulares que identificam documentos.
- `follow_url_patterns`: páginas HTML que podem ser seguidas.

O coletor rejeita redirecionamentos para outro domínio. Caso o portal entregue arquivos por um domínio CDN legítimo, esse domínio deve ser tratado como uma fonte separada e avaliado antes de qualquer alteração. Não amplie a regra para “aceitar qualquer lugar”, a clássica solução que transforma proteção em decoração.

## 7. Executar os testes

```bash
python -m venv .venv
```

Linux ou macOS:

```bash
source .venv/bin/activate
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Instale e teste:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
PYTHONPATH=src pytest -q
```

No Windows PowerShell:

```powershell
$env:PYTHONPATH = "src"
pytest -q
```

Os testes não acessam portais reais.

## 8. Primeira execução em DRY_RUN

1. Abra **Actions**.
2. Escolha **Public document collector**.
3. Clique em **Run workflow**.
4. Mantenha `dry_run=true`.
5. Execute.

O modo `DRY_RUN`:

- consulta `robots.txt`;
- visita somente as páginas permitidas;
- descobre URLs;
- normaliza URLs;
- detecta duplicidades;
- grava a fila e o estado de controle;
- não baixa documentos;
- não envia documentos às pastas `Contratos`, `Empenhos` ou `Processos`.

Arquivos operacionais de estado e logs podem ser gravados no Drive, pois são necessários para provar que o DRY_RUN ocorreu e para preservar a fila.

Uma execução real é recusada enquanto o banco não contiver o marcador `dry_run_completed=1`.

## 9. Executar manualmente de forma real

Depois de revisar o DRY_RUN:

1. Abra o workflow novamente.
2. Selecione `dry_run=false`.
3. Execute.

Fluxo:

```text
GitHub Actions inicia
→ testa o projeto
→ verifica o bloqueio compartilhado
→ carrega o banco SQLite do Drive
→ verifica bloqueios persistentes
→ descobre documentos novos
→ baixa um arquivo
→ valida o PDF
→ calcula SHA-256
→ verifica duplicidade
→ envia ao Drive
→ grava o ID retornado
→ sincroniza o estado
→ aguarda entre 25 e 45 segundos
→ repete até o limite
→ cria backup do estado
→ libera o bloqueio
→ encerra
```

## 10. Habilitar o agendamento

O workflow contém apenas um agendamento por dia:

```yaml
schedule:
  - cron: "23 10 * * *"
```

O cron usa UTC. Esse horário corresponde normalmente a 07:23 no horário de Brasília.

O job agendado permanece desativado até a criação da variável:

```text
COLLECTOR_SCHEDULE_ENABLED=true
```

Crie em:

```text
Settings
→ Secrets and variables
→ Actions
→ Variables
→ New repository variable
```

Não habilite essa variável antes de concluir e revisar o primeiro DRY_RUN.

## 11. Consultar logs

Os logs ficam disponíveis em dois lugares:

1. Artefato do GitHub Actions por 30 dias.
2. Subpasta `Logs` no Google Drive.

Formato JSON Lines:

```json
{"time":"2026-07-16T10:23:29Z","level":"INFO","message":"documento concluído","domain":"exemplo.gov.br","http_status":200,"size":184233,"sha256":"...","drive_file_id":"...","daily_count":1}
```

Os logs incluem:

- horário;
- workflow;
- domínio;
- URL;
- status HTTP;
- tamanho;
- SHA-256;
- ID do Drive;
- duração da requisição;
- intervalo aplicado;
- pausa periódica;
- contagem diária;
- motivo de encerramento.

O filtro de logs remove valores secretos conhecidos e padrões de token. Ainda assim, não adicione código que imprima objetos de credenciais.

## 12. Persistência e retomada da fila

O banco `collector-state.sqlite3` registra:

- URL original;
- URL normalizada;
- URL final;
- identificador;
- domínio;
- nome;
- SHA-256;
- tamanho;
- ETag;
- Last-Modified;
- ID do arquivo no Drive;
- data do download;
- status;
- tentativas;
- erro.

Antes de cada substituição final, o arquivo anterior é copiado como backup.

Depois de cada documento concluído, o banco é sincronizado novamente. Se o runner for interrompido:

- documentos já confirmados permanecem registrados;
- itens incompletos continuam pendentes ou em retry;
- arquivos parciais não são enviados;
- um upload concluído sem atualização do SQLite ainda é detectável pelo SHA-256 armazenado nas propriedades do arquivo no Drive.

A próxima execução retoma automaticamente a fila.

## 13. Bloqueio compartilhado

O arquivo `Estado/collector-lock.json` contém:

```json
{
  "owner": "github-actions",
  "workflow_run_id": "123456789",
  "started_at": "2026-07-16T10:23:00+00:00",
  "expires_at": "2026-07-16T13:23:00+00:00",
  "domain": "exemplo.gov.br"
}
```

O lock usa atualização otimista por ETag. Se outro processo alterar o arquivo, a gravação é recusada e o coletor relê o estado.

O lock é atualizado durante a execução e liberado no bloco `finally`, inclusive após erro.

O `concurrency` do GitHub também impede dois workflows simultâneos no mesmo repositório:

```yaml
concurrency:
  group: public-document-collector
  cancel-in-progress: false
```

O lock no Drive continua necessário porque o sistema local não participa do `concurrency` do GitHub.

## 14. Coordenar com o sistema local

O sistema local deve usar:

- a mesma pasta raiz no Drive;
- o mesmo `collector-lock.json`;
- o mesmo `daily-budget.json`;
- o mesmo limite diário;
- um `owner` diferente, por exemplo `local`.

Variáveis locais:

```text
COLLECTOR_OWNER=local
GOOGLE_DRIVE_FOLDER_ID=...
GOOGLE_SERVICE_ACCOUNT_JSON=...
```

Antes de iniciar qualquer coleta local:

1. adquirir o lock no Drive;
2. confirmar que o proprietário atual não é `github-actions`;
3. atualizar o orçamento diário compartilhado;
4. renovar o lock durante trabalhos longos;
5. liberar no final.

Não execute uma versão local antiga que ignore esses arquivos. Um “bloqueio compartilhado” respeitado por apenas metade dos participantes é só um arquivo decorativo com autoestima.

## 15. Limite diário compartilhado

`Estado/daily-budget.json` armazena por data UTC e domínio:

```json
{
  "version": 1,
  "days": {
    "2026-07-16": {
      "exemplo.gov.br": {
        "requests": 18,
        "downloads": 12
      }
    }
  }
}
```

O limite inicial é:

```text
MAX_FILES_PER_DOMAIN_PER_DAY=200
```

O contador precisa ser atualizado tanto pelo GitHub Actions quanto pelo coletor local.

## 16. HTTP 403

Ao receber 403:

- interrompe imediatamente;
- grava o domínio como bloqueado no SQLite;
- não inicia outro runner automaticamente;
- não tenta outro IP;
- não usa proxy, Tor ou VPN;
- não reinicia automaticamente;
- exige liberação manual.

Antes de liberar, confirme com o administrador do portal se o acesso pode ser retomado e revise o ritmo.

Para liberar localmente:

```bash
PYTHONPATH=src python -m collector.main release-domain exemplo.gov.br
```

No PowerShell:

```powershell
$env:PYTHONPATH = "src"
python -m collector.main release-domain exemplo.gov.br
```

O comando exige as mesmas credenciais do Drive configuradas no ambiente.

## 17. HTTP 429

Ao receber 429:

- interrompe imediatamente;
- lê `Retry-After` quando presente;
- grava a data permitida;
- recusa nova execução antes desse prazo.

Depois do prazo, o bloqueio 429 pode ser liberado automaticamente pelo coletor. Caso não exista `Retry-After`, trate como bloqueio manual e investigue antes de continuar.

## 18. Erros HTTP 408 e 5xx

Códigos tratados:

```text
408, 500, 502, 503, 504
```

São feitas no máximo três esperas progressivas, aproximadamente:

```text
60 segundos
180 segundos
600 segundos
```

Cada espera recebe pequena variação aleatória. Depois do limite, o item é marcado para retry ou falha conforme o número total de tentativas.

## 19. Liberar um lock manualmente

Primeiro, confirme que não existe workflow nem coletor local em execução.

Para liberar um lock expirado:

```bash
PYTHONPATH=src python -m collector.main release-lock
```

Para forçar a liberação de um lock ainda válido:

```bash
PYTHONPATH=src python -m collector.main release-lock --force
```

Use `--force` somente depois de confirmar que o proprietário anterior morreu, travou ou foi encerrado. Liberar um lock ativo é uma forma sofisticada de pedir corrupção de estado.

## 20. Validação de PDF

Antes do upload:

1. verifica o tamanho;
2. confirma que começa por `%PDF-`;
3. rejeita HTML salvo como `.pdf`;
4. valida o `Content-Type`;
5. tenta abrir a estrutura com `pypdf`;
6. exige pelo menos uma página;
7. calcula SHA-256;
8. verifica duplicidade por hash;
9. somente então envia ao Drive.

Arquivos `.partial` são locais e nunca enviados.

## 21. Duplicidades

A prevenção ocorre em três níveis:

1. URL normalizada única na fila.
2. SHA-256 único no SQLite.
3. Propriedade `sha256` no arquivo do Google Drive.

Quando disponível, o coletor envia:

```text
If-None-Match
If-Modified-Since
```

Uma resposta 304 marca o item como não modificado sem novo upload.

## 22. Classificação das pastas

- Tipo contendo `contrat` vai para `Contratos`.
- Tipo contendo `empenh` vai para `Empenhos`.
- Outros tipos vão para `Processos`.

Essa regra está em `src/collector/main.py` e pode ser adaptada sem alterar o mecanismo de segurança.

## 23. Variáveis principais

```text
MIN_DELAY_SECONDS=25
MAX_DELAY_SECONDS=45
PAUSE_EVERY_DOWNLOADS=10
MIN_PAUSE_SECONDS=300
MAX_PAUSE_SECONDS=600
MAX_FILES_PER_RUN=40
MAX_FILES_PER_DOMAIN_PER_DAY=200
MAX_PAGES_PER_RUN=25
MAX_QUEUE_SIZE=500
MAX_FILE_SIZE_BYTES=104857600
REQUEST_TIMEOUT_SECONDS=45
MAX_REDIRECTS=5
LOCK_TTL_MINUTES=180
```

Não aumente limites até observar vários ciclos estáveis e, idealmente, obter orientação do responsável pelo portal.

## 24. O que o projeto não faz

- Não resolve CAPTCHA.
- Não autentica em área restrita.
- Não contorna bloqueios.
- Não troca IP.
- Não usa múltiplas contas.
- Não usa múltiplos runners.
- Não coleta fontes desabilitadas.
- Não executa JavaScript para descobrir links.
- Não interpreta páginas específicas sem configuração.
- Não garante que um portal permita automação só porque o documento é público.

Documento público não significa infraestrutura sem limite. A publicidade do conteúdo e a forma de acesso são questões diferentes, distinção que servidores aprendem rapidamente e scripts aprendem depois de causar 20 mil requisições.

## 25. Checklist antes de ativar

- [ ] Repositório privado criado.
- [ ] Google Drive API ativada.
- [ ] Conta de serviço criada.
- [ ] Somente a pasta necessária compartilhada.
- [ ] Secrets adicionados.
- [ ] Fonte real revisada e explicitamente habilitada.
- [ ] `robots.txt` conferido.
- [ ] DRY_RUN executado.
- [ ] URLs descobertas revisadas.
- [ ] Testes passando.
- [ ] Sistema local atualizado para usar o mesmo lock e orçamento.
- [ ] Nenhum bloqueio 403 ativo.
- [ ] Agendamento habilitado somente depois da validação manual.

## Licença e responsabilidade

Adicione a licença adequada antes de publicar o repositório. O operador é responsável por verificar termos de uso, `robots.txt`, limites técnicos e orientações administrativas de cada fonte.
