# Arena Pro — hierarquia alinhada ao Arena Run (27/10/2026)

## Fonte de referência
Arena Run separa Arena Brain (Master), Arena Coach (organizador), Arena Run (atleta).
No Arena Run, apenas o Brain cria organizadores; cadastro público de organizador fica desativado; acesso provisório exige troca de senha no primeiro login.
Organizador cria evento em rascunho e solicita aprovação; Brain revisa e publica. Organizadores enxergam apenas seus dados.

## Modelo Arena Pro
- Master: cria/desativa organizadores, concessão/revogação de acesso, revisão de primeiro campeonato, aprovação de publicação e autonomia posterior, auditoria financeira.
- Organizador: acesso à própria organização; gerencia eventos em rascunho, Gi/No-Gi, sexo, idade, faixa, peso, valores e prazos. Submete evento para avaliação. Não publica sem autorização válida. Não vê informações de outras organizações.
- Atleta: consulta eventos publicados, inscreve-se e vê pagamento pendente/confirmado, sua categoria de peso e chaves publicadas; não altera registros financeiros nem chaves.

## Operação e segurança
1. Desabilitar criação pública de organizador **somente ao disponibilizar** o fluxo Master completo e testado. Atualmente existem rotas /api/public/organizer-signup e /api/organizer-onboarding; bloqueá-las na API, não apenas esconder links.
2. Master cria a organização e emite convite de uso único. Senha permanente deve ser criada pelo usuário com padrão forte. Se houver código provisório de seis dígitos, usar código de ativação de curta duração com tentativas limitadas, expiração, armazenamento hash e troca obrigatória da senha; nunca senha permanente de 6 dígitos.
3. Separar status de conta/organização (convite, ativo, suspenso), revisão do evento (rascunho, enviado, ajustes solicitados, aprovado, publicado) e status financeiro da inscrição (pendente, confirmado, cancelado, reembolsado).
4. Uma organização aprovada não significa publicação automática do primeiro evento. Somente Master aprova a primeira publicação. Autonomia para próximos eventos é concessão explícita, revogável e auditada.
5. Chaves: apenas pagamentos confirmados antes do prazo e inscrições esportivamente válidas; vencido prazo, job idempotente gera rascunho para revisão antes de publicação. Sem reordenar chaves publicadas automaticamente.
6. Catálogo Gi/No-Gi versionado, com idade, faixa, sexo e limites de peso conforme regulamento do evento; não aplicar peso adulto a categorias infantis.

## Dependências para liberar em produção
- RBAC aplicado no backend em todas as rotas de criação, aprovação, publicação, cobrança e chaves; testes de isolamento entre organizadores.
- Fluxo Master para cadastrar organizadores e convite ativável; autenticação Neon Auth testada (inclusive verificação de e-mail).
- Tela Master com fila de aprovação de evento e concessão explícita de autonomia.
- Migração de status e prazo final de pagamento em ambiente de teste, execução de job e testes.
- Remover telas/rotas legadas de cadastro público somente após checagem de referências e migração de usuários.

Não ativar cobrança real nem excluir registros de produção como parte desta mudança de hierarquia.
