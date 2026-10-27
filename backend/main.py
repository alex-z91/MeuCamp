"""MEUCAMP 2.0 — API inicial de autenticação e autorização.

Execute atrás de HTTPS e proxy reverso na mesma origem do frontend.
Não exponha credenciais DATABASE_URL ao navegador.
"""
import os
import re
from typing import Annotated, Literal

import asyncpg
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DATABASE_URL = os.environ.get("DATABASE_URL")
NEON_AUTH_URL = os.environ.get("NEON_AUTH_URL", "").rstrip("/")
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "https://meucamp.alexcoder.space")
app = FastAPI(title="MEUCAMP API", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=[FRONTEND_ORIGIN], allow_credentials=True, allow_methods=["GET", "POST", "PATCH"], allow_headers=["Content-Type", "Authorization"])


@app.on_event("startup")
async def start() -> None:
    if not DATABASE_URL or not NEON_AUTH_URL:
        raise RuntimeError("Configure DATABASE_URL e NEON_AUTH_URL no servidor")
    app.state.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)


@app.on_event("shutdown")
async def shutdown() -> None:
    await app.state.pool.close()


def valid_neon_cookie_name(name: str) -> bool:
    # Neon Auth pode usar um prefixo próprio; exigir nome sintaticamente seguro.
    return bool(re.fullmatch(r"(?:__Secure-)?[A-Za-z0-9_.-]*session_token", name))


class Identity(BaseModel):
    user_id: str
    email: str
    email_verified: bool = False


async def authenticated(request: Request) -> Identity:
    # Validação no servidor a cada requisição. Não confie em roles enviadas pelo frontend.
    token = request.cookies.get("meucamp_session")
    cookie_name = request.cookies.get("meucamp_session_name", "better-auth.session_token")
    if not valid_neon_cookie_name(cookie_name):
        raise HTTPException(401, "Cookie de sessão não reconhecido")
    cookie = f"{cookie_name}={token}" if token else ""
    if not cookie:
        raise HTTPException(401, "Autenticação necessária")
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
        response = await client.get(f"{NEON_AUTH_URL}/get-session", headers={"cookie": cookie})
    if response.status_code != 200:
        raise HTTPException(401, "Neon recusou a verificação de sessão")
    try:
        data = response.json()
        user = data.get("user") if isinstance(data, dict) else None
        uid = user.get("id") if isinstance(user, dict) else None
        email = user.get("email") if isinstance(user, dict) else None
    except (ValueError, AttributeError):
        raise HTTPException(401, "Sessão inválida")
    if not uid or not email:
        raise HTTPException(401, "Neon não retornou uma identidade ativa")
    return Identity(user_id=str(uid), email=str(email), email_verified=user.get('emailVerified') is True)


async def has_platform_role(pool, user_id: str, permission: str) -> bool:
    return bool(await pool.fetchval("""
      SELECT true FROM public.platform_access a
      JOIN public.role_permissions rp ON rp.role_key=a.role
      WHERE a.user_id=$1 AND a.enabled=true AND rp.permission_key=$2 LIMIT 1
    """, user_id, permission))


async def has_organizer_role(pool, user_id: str, organizer_id: str, permission: str) -> bool:
    return bool(await pool.fetchval("""
      SELECT true FROM public.organizer_members m
      JOIN public.organizers o ON o.id=m.organizer_id
      JOIN public.role_permissions rp ON rp.role_key=m.role
      WHERE m.user_id=$1 AND m.organizer_id=$2::uuid
        AND m.status='active' AND o.status='active'
        AND rp.permission_key=$3 LIMIT 1
    """, user_id, organizer_id, permission))


@app.get("/api/health")
async def health():
    return {"status": "ok"}


async def provision_verified_organizer(pool, user: Identity) -> int:
    """Link verified self-signups in draft; legacy applications require approval."""
    if not user.email_verified:
        return 0
    count = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            apps = await conn.fetch("""
                SELECT * FROM public.organizer_applications
                WHERE (status='approved' OR (source='self_signup' AND status IN ('requested','reviewing'))) AND lower(email)=lower($1)
                ORDER BY created_at FOR UPDATE
            """, user.email.strip())
            for app in apps:
                oid = app["organizer_id"]
                if oid is None:
                    oid = await conn.fetchval(
                        "INSERT INTO public.organizers(name,status) VALUES($1,'draft') RETURNING id",
                        app["organization_name"])
                    await conn.execute(
                        "UPDATE public.organizer_applications SET organizer_id=$1 WHERE id=$2",
                        oid, app["id"])
                await conn.execute("""
                    INSERT INTO public.organizer_members(organizer_id,user_id,role,status)
                    VALUES($1,$2,'admin','active')
                    ON CONFLICT(organizer_id,user_id) DO NOTHING
                """, oid, user.user_id)
                member = await conn.fetchrow("""
                    SELECT role,status FROM public.organizer_members
                    WHERE organizer_id=$1 AND user_id=$2
                """, oid, user.user_id)
                if member["role"] != "admin" or member["status"] != "active":
                    continue
                await conn.execute(
                    "UPDATE public.organizers SET status='active' WHERE id=$1 AND status='draft' AND EXISTS (SELECT 1 FROM public.organizer_applications WHERE id=$2 AND status='approved')", oid, app["id"])
                if not await conn.fetchval("""
                    SELECT 1 FROM public.organizer_onboarding_audit
                    WHERE application_id=$1 AND event='verified_owner_activated' LIMIT 1
                """, app["id"]):
                    await conn.execute("""
                        INSERT INTO public.organizer_onboarding_audit
                        (application_id,organizer_id,actor_user_id,event)
                        VALUES($1,$2,$3,'verified_owner_activated')
                    """, app["id"], oid, user.user_id)
                count += 1
    return count


@app.get("/api/me")
async def me(user: Annotated[Identity, Depends(authenticated)], request: Request):
    pool = request.app.state.pool
    await provision_verified_organizer(pool, user)
    master = await has_platform_role(pool, user.user_id, "platform.organizers.read")
    rows = await pool.fetch("""
        SELECT m.organizer_id::text AS id, o.name, o.status, m.role
        FROM public.organizer_members m JOIN public.organizers o ON o.id=m.organizer_id
        WHERE m.user_id=$1 AND m.status='active' AND o.status IN ('draft','active')
        ORDER BY o.name
    """, user.user_id)
    return {"email": user.email, "email_verified": user.email_verified, "master": master,
            "organizations": [dict(row) for row in rows]}


@app.get("/api/platform/organizers")
async def organizers(user: Annotated[Identity, Depends(authenticated)], request: Request):
    pool = request.app.state.pool
    if not await has_platform_role(pool, user.user_id, "platform.organizers.read"):
        raise HTTPException(403, "Acesso não autorizado")
    rows = await pool.fetch("SELECT id::text, name, status, created_at FROM public.organizers ORDER BY created_at DESC LIMIT 100")
    return {"items": [dict(row) for row in rows]}


@app.get("/api/organizers/{organizer_id}/permissions")
async def organizer_permissions(organizer_id: str, user: Annotated[Identity, Depends(authenticated)], request: Request):
    pool = request.app.state.pool
    try:
        import uuid
        uuid.UUID(organizer_id)
    except ValueError:
        raise HTTPException(404, "Organização não encontrada")
    if not await has_organizer_role(pool, user.user_id, organizer_id, "organizer.events.read"):
        raise HTTPException(403, "Acesso não autorizado")
    rows = await pool.fetch("""
      SELECT rp.permission_key FROM public.organizer_members m
      JOIN public.role_permissions rp ON rp.role_key=m.role
      WHERE m.user_id=$1 AND m.organizer_id=$2::uuid AND m.status='active'
    """, user.user_id, organizer_id)
    return {"permissions": [r["permission_key"] for r in rows]}


from pydantic import EmailStr
from http.cookies import SimpleCookie


class LoginInput(BaseModel):
    email: str
    password: str
    remember: bool = True


def trusted_request(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin != FRONTEND_ORIGIN:
        raise HTTPException(403, "Origem não autorizada")


@app.post("/api/auth/login")
async def login(payload: LoginInput, request: Request, response: Response):
    trusted_request(request)
    if not payload.email or not payload.password:
        raise HTTPException(400, "E-mail e senha obrigatórios")
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=False) as client:
            result = await client.post(
                f"{NEON_AUTH_URL}/sign-in/email",
                json={"email": payload.email, "password": payload.password, "rememberMe": payload.remember},
                headers={"origin": FRONTEND_ORIGIN, "content-type": "application/json"},
            )
    except httpx.RequestError:
        raise HTTPException(503, "Serviço de autenticação temporariamente indisponível")
    if result.status_code != 200:
        raise HTTPException(401, "Credenciais inválidas ou conta ainda não habilitada")
    # O cookie de identidade permanece exclusivamente em HttpOnly; não enviamos token no JSON.
    session_token = None
    session_cookie_name = None
    for header in result.headers.get_list("set-cookie"):
        parsed = SimpleCookie()
        try:
            parsed.load(header)
        except Exception:
            continue
        for name, morsel in parsed.items():
            if valid_neon_cookie_name(name):
                session_token = morsel.value
                session_cookie_name = name
                break
    if not session_token:
        raise HTTPException(502, "O provedor não retornou um cookie de sessão")
    # Exigir que o próprio provedor confirme a sessão antes de declarar login concluído.
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            check = await client.get(
                f"{NEON_AUTH_URL}/get-session",
                headers={"cookie": f"{session_cookie_name}={session_token}"},
            )
        data = check.json() if check.status_code == 200 else {}
        identity = data.get("user") if isinstance(data, dict) else None
        if not isinstance(identity, dict) or not identity.get("id"):
            raise HTTPException(502, "O provedor aceitou o login, mas não confirmou a sessão")
    except httpx.RequestError:
        raise HTTPException(503, "Não foi possível verificar a sessão no Neon")
    except ValueError:
        raise HTTPException(502, "Resposta inválida na verificação da sessão")
    response.set_cookie(
        "meucamp_session", session_token, httponly=True, secure=True,
        samesite="lax", path="/", max_age=60 * 60 * 24 * (30 if payload.remember else 1),
    )
    response.set_cookie("meucamp_session_name", session_cookie_name, httponly=True, secure=True, samesite="lax", path="/", max_age=60 * 60 * 24 * (30 if payload.remember else 1))
    # É apenas autenticação; permissões permanecem verificadas pelas rotas administrativas.
    return {"ok": True}


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    trusted_request(request)
    token = request.cookies.get("meucamp_session")
    cookie_name = request.cookies.get("meucamp_session_name", "better-auth.session_token")
    if token and valid_neon_cookie_name(cookie_name):
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
                await client.post(
                    f"{NEON_AUTH_URL}/sign-out",
                    headers={"cookie": f"{cookie_name}={token}", "origin": FRONTEND_ORIGIN},
                )
        except httpx.RequestError:
            pass
    response.delete_cookie("meucamp_session", path="/", secure=True, httponly=True, samesite="lax")
    response.delete_cookie("meucamp_session_name", path="/", secure=True, httponly=True, samesite="lax")
    return {"ok": True}


class ResetRequest(BaseModel):
    email: str


class ResetFinish(BaseModel):
    token: str
    password: str


@app.post("/api/auth/request-password-reset")
async def request_password_reset(payload: ResetRequest, request: Request):
    trusted_request(request)
    email = payload.email.strip().lower()
    if not email or len(email) > 254 or "@" not in email:
        raise HTTPException(400, "Informe um e-mail válido")
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            result = await client.post(
                f"{NEON_AUTH_URL}/request-password-reset",
                json={"email": email, "redirectTo": FRONTEND_ORIGIN + "/redefinir-senha.html"},
                headers={"origin": FRONTEND_ORIGIN, "content-type": "application/json"}
            )
    except httpx.RequestError:
        raise HTTPException(503, "Serviço de recuperação indisponível")
    if result.status_code >= 500:
        raise HTTPException(503, "Serviço de recuperação indisponível")
    # Resposta deliberadamente genérica para não revelar quais contas existem.
    return {"ok": True, "message": "Se o e-mail estiver cadastrado e habilitado, você receberá instruções."}


@app.post("/api/auth/reset-password")
async def reset_password(payload: ResetFinish, request: Request):
    trusted_request(request)
    if not payload.token or len(payload.token) > 4096:
        raise HTTPException(400, "Link inválido")
    if len(payload.password) < 12 or len(payload.password) > 128:
        raise HTTPException(400, "Use uma senha de 12 a 128 caracteres")
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            result = await client.post(
                f"{NEON_AUTH_URL}/reset-password",
                json={"token": payload.token, "newPassword": payload.password},
                headers={"origin": FRONTEND_ORIGIN, "content-type": "application/json"}
            )
    except httpx.RequestError:
        raise HTTPException(503, "Serviço de recuperação indisponível")
    if result.status_code != 200:
        raise HTTPException(400, "Link expirado ou inválido. Solicite outro.")
    return {"ok": True}

@app.get("/api/auth/session-check")
async def session_check(request: Request):
    # Diagnóstico sem revelar cookies, tokens nem contas.
    session_present = bool(request.cookies.get("meucamp_session"))
    name_present = bool(request.cookies.get("meucamp_session_name"))
    return {"cookie_present": session_present, "cookie_name_present": name_present}

# Solicitações de organizador — sem criação automática de contas ou papéis.
class OrganizerApplicationIn(BaseModel):
    organization_name: str = Field(min_length=3, max_length=180)
    contact_name: str = Field(min_length=3, max_length=180)
    email: str = Field(min_length=5, max_length=254)
    phone: str = Field(min_length=8, max_length=32)
    city: str = Field(min_length=2, max_length=120)
    state: str = Field(min_length=2, max_length=2)
    document_number: str | None = Field(default=None, max_length=18)
    consent: bool


def normalize_application(data: OrganizerApplicationIn) -> dict:
    import re as _re
    if not data.consent:
        raise HTTPException(422, "É necessário aceitar o tratamento dos dados para contato")
    email = data.email.strip().lower()
    phone = _re.sub(r"\D", "", data.phone)
    state = data.state.upper().strip()
    doc = _re.sub(r"\D", "", data.document_number or "") or None
    if "@" not in email or "." not in email.split("@")[-1] or not 10 <= len(phone) <= 13:
        raise HTTPException(422, "E-mail ou telefone inválido")
    if not _re.fullmatch(r"[A-Z]{2}", state):
        raise HTTPException(422, "UF inválida")
    return dict(organization_name=data.organization_name.strip(),
                contact_name=data.contact_name.strip(), email=email,
                phone=phone, city=data.city.strip(), state=state,
                document_number=doc)


async def submit_application(pool, data: OrganizerApplicationIn, source: str):
    p = normalize_application(data)
    try:
        row = await pool.fetchrow("""
            INSERT INTO public.organizer_applications
                (organization_name,contact_name,email,phone,city,state,document_number,source)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id::text
        """, p["organization_name"], p["contact_name"], p["email"],
             p["phone"], p["city"], p["state"], p["document_number"], source)
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "Já existe solicitação em análise para esta organização e e-mail")
    access_token = __import__("secrets").token_urlsafe(40)
    access_hash = __import__("hashlib").sha256(access_token.encode()).hexdigest()
    await pool.execute("""INSERT INTO public.organizer_application_access(application_id,token_hash)
        VALUES($1,$2)""",__import__("uuid").UUID(row["id"]),access_hash)
    return {"ok":True,"id":row["id"],"status":"requested",
            "tracking_url":FRONTEND_ORIGIN+"/acompanhar-acesso.html?token="+access_token}


class OrganizerSelfSignup(OrganizerApplicationIn):
    password: str = Field(min_length=12, max_length=128)


@app.post("/api/public/organizer-signup", status_code=201)
async def organizer_self_signup(payload: OrganizerSelfSignup, request: Request):
    """Register identity via Neon Auth, then open an approval request.
    Email verification is required before linking this identity to a draft organizer.
    Approval alone controls publication, never account registration.
    """
    trusted_request(request)
    raise HTTPException(403, "O cadastro de organizadores é exclusivo do Master. Solicite um convite à equipe Arena Pro.")
    normalized = normalize_application(payload)
    existing = await request.app.state.pool.fetchval("SELECT 1 FROM public.organizer_applications WHERE lower(email)=lower($1) AND status IN ('requested','reviewing','approved')",normalized["email"])
    if existing:
        raise HTTPException(409,"Já existe solicitação para este e-mail. Entre na sua conta ou acompanhe sua solicitação.")
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        try:
            upstream = await client.post(
                f"{NEON_AUTH_URL}/sign-up/email",
                json={"email":normalized["email"],"password":payload.password,
                      "name":normalized["contact_name"]},
                headers={"origin":FRONTEND_ORIGIN,"content-type":"application/json"})
        except httpx.RequestError:
            raise HTTPException(503,"Serviço de contas indisponível")
    if upstream.status_code not in (200, 201):
        # Auth provider details may reveal account enumeration or internal infrastructure.
        # Provide actionable but safe messages, preserving distinct retryable failures.
        if upstream.status_code == 429:
            raise HTTPException(429, "Muitas tentativas de cadastro. Aguarde alguns minutos e tente novamente.")
        if upstream.status_code in (400, 409, 422):
            raise HTTPException(409, "O cadastro não foi aceito. Se você já possui uma conta, utilize Entrar ou Recuperar senha. Confira também os requisitos da senha.")
        if upstream.status_code == 403:
            raise HTTPException(503, "O serviço de autenticação recusou o cadastro. Entre em contato com o suporte da Arena Pro.")
        raise HTTPException(502, "Não foi possível concluir o cadastro no serviço de autenticação. Tente novamente mais tarde.")
    result = await submit_application(request.app.state.pool, payload, "self_signup")
    return {"ok":True,"status":"awaiting_email_verification",
            "message":"Conta criada. Confirme seu e-mail e faça login para preparar o campeonato.",
            "tracking_url":result["tracking_url"]}


@app.post("/api/auth/resend-verification")
async def resend_organizer_verification(request: Request,
    user: Annotated[Identity, Depends(authenticated)]):
    trusted_request(request)
    if user.email_verified:
        return {"ok":True,"already_verified":True}
    cookie_name=request.cookies.get("meucamp_session_name","better-auth.session_token")
    token=request.cookies.get("meucamp_session")
    if not token or not valid_neon_cookie_name(cookie_name):
        raise HTTPException(401,"Entre novamente para confirmar seu e-mail.")
    try:
        async with httpx.AsyncClient(timeout=12,follow_redirects=False) as client:
            res=await client.post(f"{NEON_AUTH_URL}/send-verification-email",
                json={"email":user.email,"callbackURL":FRONTEND_ORIGIN+"/completar-organizacao.html"},
                headers={"origin":FRONTEND_ORIGIN,"content-type":"application/json",
                         "cookie":f"{cookie_name}={token}"})
    except httpx.RequestError:
        raise HTTPException(503,"O serviço de verificação está indisponível. Tente mais tarde.")
    if res.status_code==429:
        raise HTTPException(429,"Muitas solicitações. Aguarde antes de tentar novamente.")
    if res.status_code!=200:
        raise HTTPException(503,"O envio de verificação não está disponível. Confira a configuração de e-mails do Neon Auth.")
    return {"ok":True,"message":"Se o provedor estiver configurado, enviamos um novo link de confirmação."}


@app.post("/api/organizer-onboarding", status_code=201)
async def complete_organizer_onboarding(payload: OrganizerApplicationIn, request: Request,
    user: Annotated[Identity, Depends(authenticated)]):
    trusted_request(request)
    raise HTTPException(403, "O cadastro de organizadores é exclusivo do Master. Solicite um convite à equipe Arena Pro.")
    if not user.email_verified:
        raise HTTPException(403, "Confirme seu e-mail antes de continuar.")
    data = normalize_application(payload)
    if data["email"] != user.email.strip().lower():
        raise HTTPException(422, "Use o e-mail da conta conectada.")
    pool = request.app.state.pool
    current = await pool.fetchval(
        "SELECT 1 FROM public.organizer_members WHERE user_id=$1 AND status='active' LIMIT 1", user.user_id)
    if current:
        raise HTTPException(409, "Você já possui uma organização. Atualize o painel.")
    pending = await pool.fetchval(
        "SELECT 1 FROM public.organizer_applications WHERE lower(email)=lower($1) AND status IN ('requested','reviewing','approved') LIMIT 1", user.email)
    if pending:
        raise HTTPException(409, "Existe uma solicitação para este e-mail. Entre em contato com o suporte.")
    await submit_application(pool, payload, "self_signup")
    await provision_verified_organizer(pool, user)
    return {"ok": True, "status": "draft"}


@app.post("/api/public/organizer-applications", status_code=201)
async def create_application(payload: OrganizerApplicationIn, request: Request):
    trusted_request(request)
    raise HTTPException(403, "O cadastro de organizadores é exclusivo do Master. Solicite um convite à equipe Arena Pro.")
    return await submit_application(request.app.state.pool, payload, "public")


@app.post("/api/platform/organizer-applications", status_code=201)
async def master_create_application(payload: OrganizerApplicationIn, request: Request,
                                    user: Annotated[Identity, Depends(authenticated)]):
    trusted_request(request)
    if not await has_platform_role(request.app.state.pool, user.user_id, "platform.organizers.manage"):
        raise HTTPException(403, "Acesso não autorizado")
    return await submit_application(request.app.state.pool, payload, "master")


@app.post("/api/platform/organizers/create-with-invite", status_code=201)
async def master_create_organizer_with_invite(payload: OrganizerApplicationIn,
    request: Request, user: Annotated[Identity, Depends(authenticated)]):
    """Create an approved organization and a single-use secure invitation.
    Password is set by the invited user through Neon Auth, not by the Master.
    """
    trusted_request(request)
    pool=request.app.state.pool
    if not await has_platform_role(pool,user.user_id,"platform.organizers.manage"):
        raise HTTPException(403,"Acesso Master necessário")
    p=normalize_application(payload)
    import secrets as _secrets,hashlib as _hashlib,uuid as _uuid
    token=_secrets.token_urlsafe(40)
    digest=_hashlib.sha256(token.encode()).hexdigest()
    async with pool.acquire() as conn:
      async with conn.transaction():
        exists=await conn.fetchval("""SELECT 1 FROM public.organizer_applications
          WHERE lower(email)=lower($1) AND status IN ('requested','reviewing','approved')""",p["email"])
        if exists:raise HTTPException(409,"Já existe cadastro ou solicitação para este e-mail.")
        oid=await conn.fetchval("INSERT INTO public.organizers(name,status) VALUES($1,'draft') RETURNING id",p["organization_name"])
        aid=await conn.fetchval("""INSERT INTO public.organizer_applications
          (organization_name,contact_name,email,phone,city,state,document_number,source,status,
           organizer_id,reviewed_by,reviewed_at)
          VALUES($1,$2,$3,$4,$5,$6,$7,'master','approved',$8,$9,now()) RETURNING id""",
          p["organization_name"],p["contact_name"],p["email"],p["phone"],p["city"],
          p["state"],p["document_number"],oid,user.user_id)
        await conn.execute("""INSERT INTO public.access_invites
          (email,scope,organizer_id,role,expires_at,token_hash,application_id)
          VALUES($1,'organizer',$2,'admin',now()+interval '7 days',$3,$4)""",
          p["email"],oid,digest,aid)
        await conn.execute("""INSERT INTO public.organizer_onboarding_audit
          (application_id,organizer_id,actor_user_id,event)
          VALUES($1,$2,$3,'master_created_organizer')""",aid,oid,user.user_id)
    return {"ok":True,"organizer_id":str(oid),
            "invite_url":FRONTEND_ORIGIN+"/aceitar-convite.html?token="+token,
            "message":"Organização criada. Envie o convite de ativação ao responsável."}


@app.get("/api/platform/organizer-applications")
async def master_list_applications(request: Request,
                                   user: Annotated[Identity, Depends(authenticated)]):
    if not await has_platform_role(request.app.state.pool, user.user_id, "platform.organizers.read"):
        raise HTTPException(403, "Acesso não autorizado")
    rows = await request.app.state.pool.fetch("""
        SELECT id::text,organization_name,contact_name,email,phone,city,state,
               source,status,created_at
        FROM public.organizer_applications ORDER BY created_at DESC LIMIT 100
    """)
    return {"items": [dict(r) for r in rows]}


async def send_organizer_approval_email(recipient,contact,organization,url):
    import asyncio,smtplib,ssl
    from email.message import EmailMessage
    from email.utils import formataddr
    host=os.environ["MEUCAMP_SMTP_HOST"];sender=os.environ["MEUCAMP_SMTP_FROM"]
    port=int(os.getenv("MEUCAMP_SMTP_PORT","465"))
    user=os.getenv("MEUCAMP_SMTP_USER","");password=os.getenv("MEUCAMP_SMTP_PASSWORD","")
    msg=EmailMessage();msg["From"]=formataddr(("Arena Pro",sender));msg["To"]=recipient
    msg["Subject"]="Arena Pro — acesso aprovado"
    msg.set_content(f"Olá, {contact}!\n\nSua organização {organization} foi aprovada.\nAtive sua conta sem refazer o cadastro:\n{url}\n\nEste convite expira em 7 dias.\nEquipe Arena Pro")
    def deliver():
        if port==465:
            with smtplib.SMTP_SSL(host,port,timeout=12,context=ssl.create_default_context()) as server:
                if user:server.login(user,password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host,port,timeout=12) as server:
                server.starttls(context=ssl.create_default_context())
                if user:server.login(user,password)
                server.send_message(msg)
    await asyncio.to_thread(deliver)

async def send_meucamp_email(recipient, contact, organization, url):
    """Deliver Arena Pro transactional email over Resend HTTPS, with SMTP fallback."""
    from html import escape
    test = organization.startswith("TESTE")
    subject = "Arena Pro — teste de envio" if test else "Arena Pro — sua organização foi aprovada"
    body = ("Este é um teste de entrega. Nenhum convite foi criado." if test
            else f"Sua organização {organization} foi aprovada. Ative sua conta: {url}\nO convite expira em 7 dias.")
    text_body = f"Olá, {contact}!\n\n{body}\n\nEquipe Arena Pro"
    if not os.getenv("RESEND_API_KEY"):
        await send_organizer_approval_email(recipient, contact, organization, url)
        return "smtp"
    from_email = os.getenv("RESEND_FROM_EMAIL", "Arena Pro <convites@alexcoder.space>")
    reply_to = os.getenv("RESEND_REPLY_TO", "alexdesigner.one@gmail.com")
    detail = ("Este é um teste de envio pela Arena Pro. Nenhum convite foi gerado." if test
              else f"A organização <strong>{escape(organization)}</strong> foi aprovada. Sua conta está pronta para ativação.")
    action = ("" if test else '<p style="margin:30px 0"><a href="'+escape(url,quote=True)+'" style="background:#ff9825;color:#191919;font-weight:600;text-decoration:none;border-radius:7px;padding:13px 24px;display:inline-block">Ativar meu acesso</a></p><p style="font-size:12px;color:#666">Este link expira em 7 dias. Não compartilhe com outras pessoas.</p>')
    html = ('<div style="background:#f6f6f6;padding:36px 12px;font-family:Arial,sans-serif;color:#242424">'
            '<div style="max-width:560px;background:#fff;margin:auto;border-radius:12px;border:1px solid #eee;padding:32px">'
            '<div style="font-size:26px;font-weight:600;color:#191919"><span style="color:#ff9825">arena</span> pro</div>'
            '<div style="height:3px;width:44px;background:#ff9825;margin:20px 0"></div>'
            '<h1 style="font-size:23px;font-weight:600">Olá, '+escape(contact)+'!</h1>'
            '<p style="font-size:15px;line-height:1.7">'+detail+'</p>'+action+
            '<p style="font-size:12px;color:#777;margin-top:30px">Arena Pro · Gestão de campeonatos e eventos esportivos</p>'
            '</div></div>')
    async with httpx.AsyncClient(timeout=15,follow_redirects=False) as client:
        res=await client.post("https://api.resend.com/emails",
            headers={"Authorization":"Bearer "+os.environ["RESEND_API_KEY"],"Content-Type":"application/json"},
            json={"from":from_email,"to":[recipient],"reply_to":reply_to,
                  "subject":subject,"text":text_body,"html":html})
    if res.status_code not in (200,201):
        import logging
        logging.getLogger("arena_pro.email").warning("Resend returned HTTP %s",res.status_code)
        raise RuntimeError("email_provider_rejected")
    return "resend"

@app.post("/api/platform/test-smtp")
async def master_test_smtp(request:Request,user:Annotated[Identity,Depends(authenticated)]):
    trusted_request(request)
    if not await has_platform_role(request.app.state.pool,user.user_id,"platform.organizers.manage"):
        raise HTTPException(403,"Acesso não autorizado")
   
    required=[] if os.getenv("RESEND_API_KEY") else ["MEUCAMP_SMTP_HOST","MEUCAMP_SMTP_FROM","MEUCAMP_SMTP_USER","MEUCAMP_SMTP_PASSWORD"]
    missing=[key for key in required if not os.getenv(key)]
    if missing:return {"ok":False,"status":"not_configured","missing":missing}
    try:
        provider=await send_meucamp_email(user.email,"administrador MEUCAMP",
            "TESTE DE SMTP — nenhum convite foi criado",FRONTEND_ORIGIN+"/login.html")
    except Exception as exc:
        import smtplib
        if isinstance(exc,smtplib.SMTPAuthenticationError):category="authentication"
        elif isinstance(exc,(OSError,TimeoutError)):category="connection"
        else:category="smtp_error"
        return {"ok":False,"status":category}
    return {"ok":True,"status":"accepted_by_provider","provider":provider,"recipient":user.email}


class ApplicationReview(BaseModel):
    status: Literal["reviewing", "approved", "rejected"]


@app.post("/api/platform/organizer-applications/{application_id}/review")
async def master_review_application(application_id: str, payload: ApplicationReview,
                                    request: Request,
                                    user: Annotated[Identity, Depends(authenticated)]):
    trusted_request(request)
    if not await has_platform_role(request.app.state.pool, user.user_id, "platform.organizers.manage"):
        raise HTTPException(403, "Acesso não autorizado")
    import uuid
    try:
        parsed_id = uuid.UUID(application_id)
    except ValueError:
        raise HTTPException(404, "Solicitação não encontrada")
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            app_row = await conn.fetchrow(
                "SELECT * FROM public.organizer_applications WHERE id=$1 FOR UPDATE", parsed_id)
            if not app_row:
                raise HTTPException(404, "Solicitação não encontrada")
            if app_row["status"] not in ("requested", "reviewing"):
                raise HTTPException(409, "Solicitação já encerrada")
            oid = app_row["organizer_id"]
            if payload.status == "approved" and oid is None:
                oid = await conn.fetchval(
                    "INSERT INTO public.organizers(name,status) VALUES($1,'draft') RETURNING id",
                    app_row["organization_name"])
            result = await conn.fetchrow("""
                UPDATE public.organizer_applications
                SET status=$1, reviewed_by=$2, reviewed_at=now(), organizer_id=COALESCE($4,organizer_id)
                WHERE id=$3 RETURNING id::text,status,organizer_id::text
            """, payload.status, user.user_id, parsed_id, oid)
            if payload.status == "approved" and app_row["source"]=="self_signup":
                await conn.execute("""UPDATE public.organizers o SET status='active' WHERE o.id=$1 AND o.status='draft' AND EXISTS (SELECT 1 FROM public.organizer_members m WHERE m.organizer_id=o.id AND m.role='admin' AND m.status='active')""",oid)
            if payload.status == "approved":
                await conn.execute("""
                    INSERT INTO public.organizer_onboarding_audit
                    (application_id,organizer_id,actor_user_id,event)
                    VALUES($1,$2,$3,'organization_prepared')
                """, parsed_id, oid, user.user_id)
    notification="not_applicable";activation_url=None
    if payload.status=="approved" and app_row["source"]=="self_signup":
        notification="existing_account_approved"
    elif payload.status=="approved":
        token=__import__("secrets").token_urlsafe(40)
        digest=__import__("hashlib").sha256(token.encode()).hexdigest()
        existing=await request.app.state.pool.fetchval("SELECT id FROM public.access_invites WHERE application_id=$1",parsed_id)
        if existing:
            notification="existing_invite"
        else:
            await request.app.state.pool.execute("""INSERT INTO public.access_invites
                (email,scope,organizer_id,role,expires_at,token_hash,application_id)
                VALUES($1,'organizer',$2,'admin',now()+interval '7 days',$3,$4)""",
                app_row["email"].strip().lower(),oid,digest,parsed_id)
            activation_url=FRONTEND_ORIGIN+"/aceitar-convite.html?token="+token
            if not os.getenv("RESEND_API_KEY") and (not os.getenv("MEUCAMP_SMTP_HOST") or not os.getenv("MEUCAMP_SMTP_FROM")):
                notification="email_not_configured"
            else:
                try:
                    await send_meucamp_email(app_row["email"],app_row["contact_name"],
                        app_row["organization_name"],activation_url)
                    notification="sent"
                except Exception:
                    notification="send_failed"
    return {**dict(result),"access":"awaiting_verified_owner" if payload.status=="approved" else "unchanged",
            "email_notification":notification,"activation_url":activation_url if notification!="sent" else None}

# Convites de uso único: nunca armazenar o token em texto puro no banco.
import hashlib
import secrets
import uuid


def invitation_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@app.post("/api/platform/organizer-applications/{application_id}/invite")
async def create_organizer_invite(application_id: str, request: Request,
                                  user: Annotated[Identity, Depends(authenticated)]):
    trusted_request(request)
    pool = request.app.state.pool
    if not await has_platform_role(pool, user.user_id, "platform.organizers.manage"):
        raise HTTPException(403, "Acesso não autorizado")
    try:
        aid = uuid.UUID(application_id)
    except ValueError:
        raise HTTPException(404, "Solicitação não encontrada")
    token = secrets.token_urlsafe(40)
    async with pool.acquire() as conn:
        async with conn.transaction():
            a = await conn.fetchrow(
                "SELECT * FROM public.organizer_applications WHERE id=$1 FOR UPDATE", aid)
            if not a:
                raise HTTPException(404, "Solicitação não encontrada")
            if a["status"] != "approved":
                raise HTTPException(409, "A solicitação precisa estar aprovada")
            existing = await conn.fetchval(
                "SELECT id FROM public.access_invites WHERE application_id=$1", aid)
            if existing:
                raise HTTPException(409, "Esta solicitação já teve convite emitido")
            oid = a["organizer_id"]
            if oid is None:
                oid = await conn.fetchval(
                    "INSERT INTO public.organizers(name,status) VALUES($1,'draft') RETURNING id",
                    a["organization_name"])
                await conn.execute(
                    "UPDATE public.organizer_applications SET organizer_id=$1 WHERE id=$2",
                    oid, aid)
            await conn.execute("""
                INSERT INTO public.access_invites
                (email,scope,organizer_id,role,expires_at,token_hash,application_id)
                VALUES($1,'organizer',$2,'admin',now()+interval '7 days',$3,$4)
            """, a["email"].lower(), oid, invitation_digest(token), aid)
            await conn.execute("""
                INSERT INTO public.organizer_onboarding_audit
                (application_id,organizer_id,actor_user_id,event)
                VALUES($1,$2,$3,'invite_created')
            """, aid, oid, user.user_id)
    return {"ok": True, "invite_url": FRONTEND_ORIGIN + "/aceitar-convite.html?token=" + token,
            "message": "Copie o convite agora. O link não poderá ser recuperado posteriormente."}


@app.get("/api/public/organizer-invite")
async def get_organizer_invite(token: str, request: Request):
    if len(token) > 256 or len(token) < 30:
        raise HTTPException(400, "Convite inválido")
    a = await request.app.state.pool.fetchrow("""
        SELECT i.email,o.name FROM public.access_invites i
        JOIN public.organizers o ON o.id=i.organizer_id
        WHERE i.token_hash=$1 AND i.scope='organizer' AND i.accepted_at IS NULL
          AND i.expires_at>now()
    """, invitation_digest(token))
    if not a:
        raise HTTPException(404, "Convite inválido ou expirado")
    return {"email": a["email"], "organization": a["name"]}


@app.post("/api/organizer-invite/accept")
async def accept_organizer_invite(payload: dict, request: Request,
                                  user: Annotated[Identity, Depends(authenticated)]):
    trusted_request(request)
    token = payload.get("token")
    if not isinstance(token, str) or not 30 <= len(token) <= 256:
        raise HTTPException(422, "Convite inválido")
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            invite = await conn.fetchrow("""
                SELECT * FROM public.access_invites WHERE token_hash=$1 FOR UPDATE
            """, invitation_digest(token))
            if not invite or invite["scope"] != "organizer" or invite["accepted_at"] or invite["expires_at"].timestamp() <= __import__("datetime").datetime.now(__import__("datetime").timezone.utc).timestamp():
                raise HTTPException(410, "Convite expirado ou utilizado")
            if invite["email"].strip().lower() != user.email.strip().lower():
                raise HTTPException(403, "Entre com a conta do e-mail destinatário do convite")
            if invite["role"] != "admin" or invite["organizer_id"] is None:
                raise HTTPException(409, "Convite inconsistente")
            added = await conn.fetchval("""
                INSERT INTO public.organizer_members(organizer_id,user_id,role,status)
                VALUES($1,$2,'admin','active')
                ON CONFLICT(organizer_id,user_id) DO NOTHING
                RETURNING user_id
            """, invite["organizer_id"], user.user_id)
            if not added:
                raise HTTPException(409, "Já existe vínculo para esta conta. Consulte o suporte.")
            await conn.execute("""
                UPDATE public.organizers SET status='active'
                WHERE id=$1 AND status='draft'
            """, invite["organizer_id"])
            await conn.execute("""
                UPDATE public.access_invites SET accepted_at=now() WHERE id=$1
            """, invite["id"])
            await conn.execute("""
                INSERT INTO public.organizer_onboarding_audit
                (application_id,organizer_id,actor_user_id,event)
                VALUES($1,$2,$3,'invite_accepted')
            """, invite["application_id"], invite["organizer_id"], user.user_id)
    return {"ok": True}

class InviteSignup(BaseModel):
    token: str
    name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=12, max_length=128)


@app.post("/api/auth/organizer-invite-signup")
async def organizer_invite_signup(payload: InviteSignup, request: Request, response: Response):
    trusted_request(request)
    if not 30 <= len(payload.token) <= 256:
        raise HTTPException(400, "Convite inválido")
    record = await request.app.state.pool.fetchrow("""
        SELECT email FROM public.access_invites
        WHERE token_hash=$1 AND scope='organizer'
          AND accepted_at IS NULL AND expires_at>now()
    """, invitation_digest(payload.token))
    if not record:
        raise HTTPException(410, "Convite expirado ou utilizado")
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            upstream = await client.post(
                f"{NEON_AUTH_URL}/sign-up/email",
                json={"email": record["email"], "password": payload.password,
                      "name": payload.name.strip()},
                headers={"origin": FRONTEND_ORIGIN, "content-type": "application/json"},
            )
    except httpx.RequestError:
        raise HTTPException(503, "Cadastro de acesso temporariamente indisponível")
    if upstream.status_code not in (200, 201):
        raise HTTPException(409, "Não foi possível criar a conta. Se já existe, entre com sua senha.")
    session_token, session_name = None, None
    for header in upstream.headers.get_list("set-cookie"):
        parsed = SimpleCookie()
        try:
            parsed.load(header)
        except Exception:
            continue
        for name, morsel in parsed.items():
            if valid_neon_cookie_name(name):
                session_token, session_name = morsel.value, name
                break
    if not session_token:
        return {"ok": True, "login_required": True}
    async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
        checked = await client.get(
            f"{NEON_AUTH_URL}/get-session",
            headers={"cookie": f"{session_name}={session_token}"})
    data = checked.json() if checked.status_code == 200 else None
    identity = data.get("user") if isinstance(data, dict) else None
    if not isinstance(identity, dict) or identity.get("email","").lower() != record["email"].lower():
        return {"ok": True, "login_required": True}
    response.set_cookie("meucamp_session", session_token, httponly=True, secure=True, samesite="lax", path="/", max_age=86400)
    response.set_cookie("meucamp_session_name", session_name, httponly=True, secure=True, samesite="lax", path="/", max_age=86400)
    return {"ok": True, "login_required": False}


# Finance endpoints are isolated; no Mercado Pago live charge is enabled.
from finance import router as finance_router
app.include_router(finance_router)

from sports import router as sports_router
app.include_router(sports_router)

@app.get("/api/platform/overview-stats")
async def platform_overview_stats(request: Request, user: Annotated[Identity, Depends(authenticated)]):
    if not await has_platform_role(request.app.state.pool,user.user_id,"platform.organizers.read"):
        raise HTTPException(403,"Acesso não autorizado")
    row=await request.app.state.pool.fetchrow("""
      SELECT (SELECT COUNT(*) FROM public.sport_events)::int events,
             (SELECT COUNT(*) FROM public.sport_athletes)::int athletes,
             (SELECT COUNT(*) FROM public.sport_entries)::int entries,
             (SELECT COALESCE(SUM(amount_cents),0) FROM public.finance_ledger)::bigint commission_balance_cents
    """)
    return dict(row)


class ApplicationActivation(BaseModel):
    token:str=Field(min_length=30,max_length=256)
    password:str=Field(min_length=12,max_length=128)

@app.get("/api/public/organizer-application-status")
async def organizer_application_status(token:str,request:Request):
    if not 30<=len(token)<=256:raise HTTPException(404,"Solicitação não encontrada")
    digest=__import__("hashlib").sha256(token.encode()).hexdigest()
    row=await request.app.state.pool.fetchrow("""SELECT a.status,a.organization_name,a.id
        FROM public.organizer_application_access t
        JOIN public.organizer_applications a ON a.id=t.application_id
        WHERE t.token_hash=$1""",digest)
    if not row:raise HTTPException(404,"Link de acompanhamento inválido")
    return {"status":row["status"],"organization_name":row["organization_name"]}

@app.post("/api/public/organizer-application-activate")
async def activate_organizer_application(p:ApplicationActivation,request:Request):
    trusted_request(request)
    digest=__import__("hashlib").sha256(p.token.encode()).hexdigest()
    record=await request.app.state.pool.fetchrow("""SELECT a.email,a.contact_name,a.status
        FROM public.organizer_application_access t
        JOIN public.organizer_applications a ON a.id=t.application_id
        WHERE t.token_hash=$1""",digest)
    if not record:raise HTTPException(404,"Link inválido")
    if record["status"]!="approved":raise HTTPException(409,"A organização ainda não foi aprovada")
    try:
        async with httpx.AsyncClient(timeout=12,follow_redirects=False) as client:
            upstream=await client.post(f"{NEON_AUTH_URL}/sign-up/email",
                json={"email":record["email"],"name":record["contact_name"],"password":p.password},
                headers={"origin":FRONTEND_ORIGIN,"content-type":"application/json"})
    except httpx.RequestError:
        raise HTTPException(503,"Cadastro de acesso temporariamente indisponível")
    if upstream.status_code not in (200,201):
        raise HTTPException(409,"A conta já pode existir. Use Entrar ou Recuperar senha com o e-mail informado.")
    return {"ok":True,"verification_required":True}

