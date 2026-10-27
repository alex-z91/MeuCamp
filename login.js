'use strict';
// Esta URL deve corresponder ao domínio personalizado configurado no Render.
const API = 'https://api.meucamp.alexcoder.space';
const form = document.querySelector('#login-form');
const status = document.querySelector('#status');
const submit = form.querySelector('button[type="submit"]');
document.querySelector('#toggle').addEventListener('click', () => {
  const field = document.querySelector('#password');
  field.type = field.type === 'password' ? 'text' : 'password';
  const toggle = document.querySelector('#toggle');
  toggle.setAttribute('aria-pressed', String(field.type === 'text'));
  toggle.setAttribute('aria-label', field.type === 'text' ? 'Ocultar senha' : 'Mostrar senha');
});
function feedback(message) { status.textContent = message; status.dataset.state = /erro|inválid|indisponível|recusou|não reconhecid|não possui|não foi possível|ausente/i.test(message) ? 'error' : /validado|redirecionando/i.test(message) ? 'success' : 'idle'; }
form.addEventListener('submit', async event => {
  event.preventDefault();
  submit.disabled = true;
  feedback('Verificando suas credenciais...');
  try {
    const response = await fetch(API + '/api/auth/login', {
      method: 'POST', credentials: 'include',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        email: document.querySelector('#email').value.trim(),
        password: document.querySelector('#password').value,
        remember: document.querySelector('#remember').checked
      })
    });
    if (!response.ok) {
      const failure = await response.json().catch(() => ({}));
      throw new Error(response.status === 401 ? 'Credenciais não reconhecidas.' :
        (failure.detail || 'Não foi possível autenticar.') + ' (HTTP ' + response.status + ')');
    }
    const meResponse = await fetch(API + '/api/me', { credentials: 'include', cache: 'no-store' });
    if (!meResponse.ok) {
      const failure = await meResponse.json().catch(() => ({}));
      const diagnosis = await fetch(API + '/api/auth/session-check', {credentials:'include',cache:'no-store'}).then(r=>r.json()).catch(()=>null);
      const detail = diagnosis ? (diagnosis.cookie_present ? 'Cookie presente; Neon recusou ou não confirmou a sessão.' : 'Cookie ausente no navegador ou não enviado à API.') : 'Não foi possível verificar cookies.';
      throw new Error('Sessão não reconhecida (HTTP ' + meResponse.status + '). ' + (failure.detail || '') + ' ' + detail);
    }
    const me = await meResponse.json();
    if (me.master) {
      feedback('Acesso Master validado. Redirecionando...');
      window.location.assign('plataforma.html');
    } else if (Array.isArray(me.organizations) && me.organizations.length) {
      feedback('Acesso de organizador validado. Redirecionando...');
      window.location.assign('admin.html');
    } else {
      feedback('Conta autenticada, mas ainda não há vínculo com uma organização. O acesso é concedido pelo Master por convite.'); const link=document.createElement('a');link.href='login.html';link.textContent='Aguarde o convite do Master ou entre com a conta convidada';link.style.cssText='display:block;margin-top:12px;font-weight:700;color:#b75b00;text-decoration:underline';status.append(link);
    }
  } catch (error) {
    feedback(error instanceof TypeError
      ? 'API indisponível. O subdomínio api.meucamp.alexcoder.space precisa estar configurado no Render e no DNS.'
      : error.message);
  } finally {
    submit.disabled = false;
  }
});
