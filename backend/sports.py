"""Sport registration API: organizer-scoped, server-authorized, pending by default."""
import uuid
from datetime import date, datetime
from typing import Literal
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from finance import authorized, commission, cap_for_ticket, policy

router=APIRouter(prefix="/api/sports",tags=["sports"])
class EventIn(BaseModel):
    organizer_id:uuid.UUID
    name:str=Field(min_length=3,max_length=180)
    event_date:date
    city:str=Field(min_length=2,max_length=120)
    has_gi:bool=True
    has_nogi:bool=False
    has_absolute:bool=False
    registration_deadline:datetime|None=None
    payment_deadline:datetime|None=None
class AthleteIn(BaseModel):
    organizer_id:uuid.UUID
    full_name:str=Field(min_length=3,max_length=160)
    birth_date:date
    team:str=Field(default="",max_length=120)
    belt:str=Field(min_length=2,max_length=40)
    weight_grams:int=Field(ge=10000,le=300000)
    sex:Literal['male','female']
class CategoryIn(BaseModel):
    organizer_id:uuid.UUID
    event_id:uuid.UUID
    name:str=Field(min_length=2,max_length=120)
    belt:str=Field(min_length=2,max_length=40)
    min_weight_grams:int=Field(ge=0,le=300000)
    max_weight_grams:int=Field(ge=0,le=300000)
    min_birth_year:int=Field(ge=1900,le=2100)
    max_birth_year:int=Field(ge=1900,le=2100)
    sex:Literal['male','female']
class EntryIn(BaseModel):
    organizer_id:uuid.UUID
    event_id:uuid.UUID
    athlete_id:uuid.UUID
    category_id:uuid.UUID
    base_cents:int=Field(ge=0,le=100000000)
class ExternalConfirmation(BaseModel):
    organizer_id:uuid.UUID

def require_origin(request):
    from main import trusted_request
    trusted_request(request)

@router.post("/events",status_code=201)
async def create_event(p:EventIn,request:Request):
    require_origin(request)
    user=await authorized(request,p.organizer_id,"organizer.events.manage")
    if not p.has_gi and not p.has_nogi:
        raise HTTPException(422,"Selecione Gi, No-Gi ou ambos")
    if p.payment_deadline and not p.registration_deadline:
        raise HTTPException(422,"Defina o prazo de inscrições antes do prazo de pagamento")
    if p.payment_deadline and p.registration_deadline and p.payment_deadline <= p.registration_deadline:
        raise HTTPException(422,"O prazo de pagamento deve ser posterior ao encerramento das inscrições")
    async with request.app.state.pool.acquire() as conn:
      async with conn.transaction():
        row=await conn.fetchrow("""INSERT INTO public.sport_events
            (organizer_id,name,event_date,city,created_by,has_gi,has_nogi,has_absolute,registration_deadline,payment_deadline)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id::text,status""",
            p.organizer_id,p.name.strip(),p.event_date,p.city.strip(),user.user_id,
            p.has_gi,p.has_nogi,p.has_absolute,p.registration_deadline,p.payment_deadline)
        created=await build_default_categories(conn,p.organizer_id,uuid.UUID(row["id"]),p.event_date.year,
            [m for m,enabled in (("gi",p.has_gi),("nogi",p.has_nogi)) if enabled],p.has_absolute)
    return {**dict(row),"categories_created":created}

@router.get("/events/{organizer_id}")
async def list_events(organizer_id:uuid.UUID,request:Request):
    await authorized(request,organizer_id,"organizer.events.read")
    rows=await request.app.state.pool.fetch("""SELECT id::text,name,event_date,city,status,has_gi,has_nogi,has_absolute,registration_deadline,payment_deadline,registration_closed_at,public_roster_enabled FROM public.sport_events
        WHERE organizer_id=$1 ORDER BY event_date DESC LIMIT 100""",organizer_id)
    return {"items":[dict(row) for row in rows]}

@router.post("/athletes",status_code=201)
async def create_athlete(p:AthleteIn,request:Request):
    require_origin(request)
    user=await authorized(request,p.organizer_id,"organizer.registrations.manage")
    if p.birth_date>date.today():raise HTTPException(422,"Data de nascimento inválida")
    row=await request.app.state.pool.fetchrow("""INSERT INTO public.sport_athletes
        (organizer_id,full_name,birth_date,team,belt,weight_grams,created_by,sex)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id::text""",p.organizer_id,p.full_name.strip(),p.birth_date,p.team.strip(),p.belt.strip(),p.weight_grams,user.user_id,p.sex)
    return dict(row)

@router.post("/categories",status_code=201)
async def create_category(p:CategoryIn,request:Request):
    require_origin(request)
    await authorized(request,p.organizer_id,"organizer.events.manage")
    if p.min_weight_grams>p.max_weight_grams or p.min_birth_year>p.max_birth_year:
        raise HTTPException(422,"Intervalo de categoria inválido")
    try:
        row=await request.app.state.pool.fetchrow("""INSERT INTO public.sport_categories
          (organizer_id,event_id,name,belt,min_weight_grams,max_weight_grams,min_birth_year,max_birth_year,sex)
          VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id::text""",p.organizer_id,p.event_id,p.name.strip(),p.belt.strip(),p.min_weight_grams,p.max_weight_grams,p.min_birth_year,p.max_birth_year,p.sex)
    except Exception as e:
        if getattr(e,"sqlstate",None)=="23503":raise HTTPException(404,"Evento não encontrado para esta organização")
        raise
    return dict(row)

@router.post("/entries",status_code=201)
async def create_entry(p:EntryIn,request:Request):
    require_origin(request)
    user=await authorized(request,p.organizer_id,"organizer.registrations.manage")
    async with request.app.state.pool.acquire() as conn:
        event=await conn.fetchrow("SELECT status FROM public.sport_events WHERE id=$1 AND organizer_id=$2",p.event_id,p.organizer_id)
        if not event or event["status"] in ("closed","cancelled"):raise HTTPException(409,"Evento indisponível")
        blocked=await conn.fetchval("SELECT registration_closed_at IS NOT NULL OR (registration_deadline IS NOT NULL AND now() >= registration_deadline) FROM public.sport_events WHERE id=$1 AND organizer_id=$2",p.event_id,p.organizer_id)
        if blocked:raise HTTPException(409,"Inscrições encerradas")
        athlete=await conn.fetchrow("SELECT birth_date,belt,weight_grams,sex FROM public.sport_athletes WHERE id=$1 AND organizer_id=$2",p.athlete_id,p.organizer_id)
        cat=await conn.fetchrow("SELECT belt,min_weight_grams,max_weight_grams,min_birth_year,max_birth_year,sex FROM public.sport_categories WHERE id=$1 AND event_id=$2 AND organizer_id=$3",p.category_id,p.event_id,p.organizer_id)
        if not athlete or not cat:raise HTTPException(404,"Atleta ou categoria não encontrado")
        if not athlete["sex"] or athlete["sex"]!=cat["sex"] or athlete["belt"].casefold()!=cat["belt"].casefold() or not(cat["min_weight_grams"]<=athlete["weight_grams"]<=cat["max_weight_grams"]) or not(cat["min_birth_year"]<=athlete["birth_date"].year<=cat["max_birth_year"]):
            raise HTTPException(422,"Atleta fora dos critérios desta categoria")
        try:
            row=await conn.fetchrow("""INSERT INTO public.sport_entries
                (organizer_id,event_id,athlete_id,category_id,reference,base_cents,created_by)
                VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id::text,payment_status,eligible""",
                p.organizer_id,p.event_id,p.athlete_id,p.category_id,str(uuid.uuid4()),p.base_cents,user.user_id)
        except Exception as e:
            if getattr(e,"sqlstate",None)=="23505":raise HTTPException(409,"Atleta já inscrito nesta categoria")
            raise
    return dict(row)

@router.get("/entries/{organizer_id}/{event_id}")
async def list_entries(organizer_id:uuid.UUID,event_id:uuid.UUID,request:Request):
    await authorized(request,organizer_id,"organizer.events.read")
    rows=await request.app.state.pool.fetch("""SELECT e.id::text,e.athlete_id::text,e.category_id::text,a.full_name,c.name AS category,e.base_cents,e.payment_status,e.eligible
        FROM public.sport_entries e JOIN public.sport_athletes a ON a.id=e.athlete_id
        JOIN public.sport_categories c ON c.id=e.category_id
        WHERE e.organizer_id=$1 AND e.event_id=$2 ORDER BY e.created_at DESC LIMIT 200""",organizer_id,event_id)
    return {"items":[dict(row) for row in rows]}

@router.post("/entries/{entry_id}/confirm-external")
async def confirm_external(entry_id:uuid.UUID,p:ExternalConfirmation,request:Request):
    require_origin(request)
    user=await authorized(request,p.organizer_id,"organizer.registrations.manage")
    pool=request.app.state.pool
    async with pool.acquire() as conn:
      async with conn.transaction():
        org=await conn.fetchrow("SELECT status FROM public.organizers WHERE id=$1 FOR UPDATE",p.organizer_id)
        if not org or org["status"]!="active":raise HTTPException(409,"Organização não ativa")
        entry=await conn.fetchrow("SELECT * FROM public.sport_entries WHERE id=$1 AND organizer_id=$2 FOR UPDATE",entry_id,p.organizer_id)
        if not entry:raise HTTPException(404,"Inscrição não encontrada")
        if entry["payment_status"]=="external_confirmed":return {"ok":True,"duplicate":True}
        cutoff=await conn.fetchrow("""SELECT payment_deadline,registration_closed_at,registration_deadline
            FROM public.sport_events WHERE id=$1 AND organizer_id=$2 FOR UPDATE""",entry["event_id"],p.organizer_id)
        if not cutoff:raise HTTPException(404,"Evento não encontrado")
        if await conn.fetchval("SELECT 1 FROM public.sport_brackets WHERE event_id=$1 LIMIT 1",entry["event_id"]):
            raise HTTPException(409,"Chaves já geradas. Solicite revisão administrativa antes de alterar pagamentos.")
        if cutoff["payment_deadline"] is not None:
            if cutoff["payment_deadline"]<=datetime.now(cutoff["payment_deadline"].tzinfo):
                raise HTTPException(409,"Prazo de pagamento encerrado. Regularizações exigem revisão administrativa.")
        elif cutoff["registration_closed_at"] or (cutoff["registration_deadline"] and cutoff["registration_deadline"]<=datetime.now(cutoff["registration_deadline"].tzinfo)):
            raise HTTPException(409,"Inscrições encerradas. Configure um prazo de pagamento separado antes de confirmar.")
        if entry["payment_status"]!="pending" or entry["base_cents"]==0:raise HTTPException(409,"Inscrição não elegível à confirmação externa")
        cfg=await policy(conn,p.organizer_id)
        cost=commission(entry["base_cents"],cfg["percent_bps"],cfg["fixed_cents"])
        cap=cfg["debt_cap_cents"] if cfg["debt_cap_cents"] is not None else cap_for_ticket(entry["base_cents"])
        debt=await conn.fetchval("SELECT COALESCE(SUM(amount_cents),0) FROM public.finance_ledger WHERE organizer_id=$1",p.organizer_id)
        if max(0,debt)+cost>cap:raise HTTPException(409,"Limite de comissão atingido. Regularize o saldo antes de confirmar.")
        finance=await conn.fetchrow("""INSERT INTO public.finance_registrations
          (organizer_id,reference,base_cents,commission_cents,athlete_fee_cents,payment_channel,payment_status,created_by)
          VALUES($1,$2,$3,$4,0,'external','confirmed',$5) RETURNING id""",
          p.organizer_id,str(entry_id),entry["base_cents"],cost,user.user_id)
        await conn.execute("""INSERT INTO public.finance_ledger
          (organizer_id,registration_id,entry_type,amount_cents,created_by)
          VALUES($1,$2,'external_commission',$3,$4)""",p.organizer_id,finance["id"],cost,user.user_id)
        await conn.execute("UPDATE public.sport_entries SET payment_status='external_confirmed',eligible=true WHERE id=$1",entry_id)
        await conn.execute("""INSERT INTO public.finance_audit(organizer_id,actor_user_id,action,reference_id)
          VALUES($1,$2,'sport_entry_external_confirmed',$3)""",p.organizer_id,user.user_id,finance["id"])
    return {"ok":True,"commission_cents":cost,"debt_cents":max(0,debt)+cost}

class UndoExternalConfirmation(BaseModel):
    organizer_id:uuid.UUID
    reason:str=Field(min_length=10,max_length=500)

@router.post("/entries/{entry_id}/reverse-external")
async def reverse_external(entry_id:uuid.UUID,p:UndoExternalConfirmation,request:Request):
    require_origin(request)
    user=await authorized(request,p.organizer_id,"organizer.registrations.manage")
    reason=p.reason.strip()
    if len(reason)<10:raise HTTPException(422,"Informe um motivo com pelo menos 10 caracteres")
    async with request.app.state.pool.acquire() as conn:
      async with conn.transaction():
        entry=await conn.fetchrow("SELECT * FROM public.sport_entries WHERE id=$1 AND organizer_id=$2 FOR UPDATE",entry_id,p.organizer_id)
        if not entry:raise HTTPException(404,"Inscrição não encontrada")
        if entry["payment_status"]!="external_confirmed":raise HTTPException(409,"Somente pagamentos presenciais confirmados podem ser revertidos aqui")
        if await conn.fetchval("SELECT 1 FROM public.sport_brackets WHERE event_id=$1 LIMIT 1",entry["event_id"]):
            raise HTTPException(409,"Chaves já geradas. A reversão exige revisão administrativa para preservar o chaveamento.")
        registration=await conn.fetchrow("""SELECT id,commission_cents FROM public.finance_registrations
            WHERE organizer_id=$1 AND reference=$2 AND payment_channel='external'
            AND payment_status='confirmed' FOR UPDATE""",p.organizer_id,str(entry_id))
        if not registration:raise HTTPException(409,"Registro financeiro não encontrado; solicite revisão administrativa")
        original=await conn.fetchval("""SELECT COALESCE(SUM(amount_cents),0) FROM public.finance_ledger
            WHERE organizer_id=$1 AND registration_id=$2""",p.organizer_id,registration["id"])
        if original<0:raise HTTPException(409,"Saldo financeiro inconsistente; solicite revisão")
        if original>0:
            await conn.execute("""INSERT INTO public.finance_ledger
              (organizer_id,registration_id,entry_type,amount_cents,created_by)
              VALUES($1,$2,'reversal',$3,$4)""",p.organizer_id,registration["id"],-original,user.user_id)
        await conn.execute("UPDATE public.finance_registrations SET payment_status='cancelled' WHERE id=$1",registration["id"])
        await conn.execute("""UPDATE public.sport_entries SET payment_status='pending',eligible=false
              WHERE id=$1 AND organizer_id=$2""",entry_id,p.organizer_id)
        await conn.execute("""INSERT INTO public.finance_audit(organizer_id,actor_user_id,action,reference_id,details)
             VALUES($1,$2,'sport_entry_external_reversed',$3,$4)""",p.organizer_id,user.user_id,registration["id"],reason)
    return {"ok":True,"reversed_commission_cents":original,"status":"pending"}

class AthleteEdit(BaseModel):
    full_name:str=Field(min_length=3,max_length=160)
    team:str=Field(default="",max_length=120)
    birth_date:date
    belt:str=Field(min_length=2,max_length=40)
    weight_grams:int=Field(ge=10000,le=300000)

@router.get("/athletes/{organizer_id}")
async def list_athletes(organizer_id:uuid.UUID,request:Request):
    await authorized(request,organizer_id,"organizer.events.read")
    rows=await request.app.state.pool.fetch("""SELECT id::text,full_name,birth_date,team,belt,weight_grams,sex
      FROM public.sport_athletes WHERE organizer_id=$1 ORDER BY created_at DESC LIMIT 500""",organizer_id)
    return {"items":[dict(row) for row in rows]}

@router.get("/categories/{organizer_id}/{event_id}")
async def list_categories(organizer_id:uuid.UUID,event_id:uuid.UUID,request:Request):
    await authorized(request,organizer_id,"organizer.events.read")
    rows=await request.app.state.pool.fetch("""SELECT id::text,name,belt,min_weight_grams,max_weight_grams,min_birth_year,max_birth_year,sex,modality,is_absolute
      FROM public.sport_categories WHERE organizer_id=$1 AND event_id=$2 ORDER BY name LIMIT 300""",organizer_id,event_id)
    return {"items":[dict(row) for row in rows]}

@router.patch("/athletes/{organizer_id}/{athlete_id}")
async def edit_athlete(organizer_id:uuid.UUID,athlete_id:uuid.UUID,p:AthleteEdit,request:Request):
    require_origin(request)
    await authorized(request,organizer_id,"organizer.registrations.manage")
    if p.birth_date>date.today():raise HTTPException(422,"Nascimento inválido")
    pool=request.app.state.pool
    async with pool.acquire() as conn:
      async with conn.transaction():
        current=await conn.fetchrow("SELECT id FROM public.sport_athletes WHERE id=$1 AND organizer_id=$2 FOR UPDATE",athlete_id,organizer_id)
        if not current:raise HTTPException(404,"Atleta não encontrado")
        entries=await conn.fetch("""SELECT e.payment_status,c.belt,c.min_weight_grams,c.max_weight_grams,c.min_birth_year,c.max_birth_year
          FROM public.sport_entries e JOIN public.sport_categories c ON c.id=e.category_id
          WHERE e.athlete_id=$1 AND e.organizer_id=$2""",athlete_id,organizer_id)
        for e in entries:
            if e["payment_status"] not in ("pending","cancelled"):
                raise HTTPException(409,"Dados esportivos de atleta confirmado exigem revisão administrativa")
            if e["payment_status"]=="pending" and not(e["belt"].casefold()==p.belt.casefold()
                and e["min_weight_grams"]<=p.weight_grams<=e["max_weight_grams"]
                and e["min_birth_year"]<=p.birth_date.year<=e["max_birth_year"]):
                raise HTTPException(409,"A edição deixaria uma inscrição pendente fora da categoria")
        await conn.execute("""UPDATE public.sport_athletes
          SET full_name=$1,team=$2,birth_date=$3,belt=$4,weight_grams=$5
          WHERE id=$6 AND organizer_id=$7""",p.full_name.strip(),p.team.strip(),p.birth_date,p.belt.strip(),p.weight_grams,athlete_id,organizer_id)
    return {"ok":True}

# Templates inspired by IBJJF adult/master Gi divisions. Weight includes kimono.
GI_WEIGHTS = {
 "male":[("Galo",57500),("Pluma",64000),("Pena",70000),("Leve",76000),("Médio",82300),("Meio-pesado",88300),("Pesado",94300),("Superpesado",100500),("Pesadíssimo",300000)],
 "female":[("Galo",48500),("Pluma",53500),("Pena",58500),("Leve",64000),("Médio",69000),("Meio-pesado",74000),("Pesado",79300),("Superpesado",300000)]
}
NO_GI_WEIGHTS = {
 "male":[("Galo",55500),("Pluma",61500),("Pena",67500),("Leve",73500),("Médio",79500),("Meio-pesado",85500),("Pesado",91500),("Superpesado",97500),("Pesadíssimo",300000)],
 "female":[("Galo",46500),("Pluma",51500),("Pena",56500),("Leve",61500),("Médio",66500),("Meio-pesado",71500),("Pesado",76500),("Superpesado",300000)]
}
# Central catalog: year-of-birth age groups and eligible belts (IBJJF-inspired).
AGE_BELTS = {
 "mighty1":(4,4,["Branca","Cinza"]),
 "mighty2":(5,5,["Branca","Cinza"]),
 "mighty3":(6,6,["Branca","Cinza"]),
 "peewee1":(7,7,["Branca","Cinza","Amarela"]),
 "peewee2":(8,8,["Branca","Cinza","Amarela"]),
 "peewee3":(9,9,["Branca","Cinza","Amarela"]),
 "junior1":(10,10,["Branca","Cinza","Amarela","Laranja"]),
 "junior2":(11,11,["Branca","Cinza","Amarela","Laranja"]),
 "junior3":(12,12,["Branca","Cinza","Amarela","Laranja"]),
 "teen1":(13,13,["Branca","Cinza","Amarela","Laranja","Verde"]),
 "teen2":(14,14,["Branca","Cinza","Amarela","Laranja","Verde"]),
 "teen3":(15,15,["Branca","Cinza","Amarela","Laranja","Verde"]),
 "juvenile1":(16,16,["Branca","Azul","Roxa"]),
 "juvenile2":(17,17,["Branca","Azul","Roxa"]),
 "adult":(18,120,["Branca","Azul","Roxa","Marrom","Preta"]),
 "master1":(30,120,["Branca","Azul","Roxa","Marrom","Preta"]),
 "master2":(36,120,["Branca","Azul","Roxa","Marrom","Preta"]),
 "master3":(41,120,["Branca","Azul","Roxa","Marrom","Preta"]),
 "master4":(46,120,["Branca","Azul","Roxa","Marrom","Preta"]),
 "master5":(51,120,["Branca","Azul","Roxa","Marrom","Preta"]),
 "master6":(56,120,["Azul","Roxa","Marrom","Preta"]),
 "master7":(61,120,["Azul","Roxa","Marrom","Preta"])
}
@router.get("/category-catalog")
async def category_catalog(request:Request):
    # Public static definitions, not organizer data.
    return {"age_groups":[{"key":key,"min_age":age[0],"max_age":age[1],"belts":age[2],
         "auto_weights_available":key in ("adult","master1","master2","master3","master4","master5","master6","master7")}
         for key,age in AGE_BELTS.items()],
        "modalities":["gi","nogi"],"sex_divisions":["male","female"],
        "notice":"Infantil e juvenil: pesos dependem de tabela específica e do evento; não aplicar pesos adultos."}

class TemplateBatch(BaseModel):
    organizer_id:uuid.UUID
    event_id:uuid.UUID
    sex:Literal["male","female"]
    age_group:Literal["adult","master1","master2","master3","master4","master5","master6","master7"]
    belts:list[Literal["Branca","Azul","Roxa","Marrom","Preta"]]=Field(min_length=1,max_length=5)
    modality:Literal["gi","nogi"]="gi"

@router.post("/categories/templates",status_code=201)
async def create_category_templates(p:TemplateBatch,request:Request):
    require_origin(request)
    await authorized(request,p.organizer_id,"organizer.events.manage")
    pool=request.app.state.pool
    async with pool.acquire() as conn:
      async with conn.transaction():
        event=await conn.fetchrow("SELECT event_date,status FROM public.sport_events WHERE id=$1 AND organizer_id=$2 FOR UPDATE",p.event_id,p.organizer_id)
        if not event:raise HTTPException(404,"Evento não encontrado")
        if event["status"] in ("closed","cancelled"):raise HTTPException(409,"Evento encerrado")
        year=event["event_date"].year
        ages={"adult":(18,120),"master1":(30,120),"master2":(36,120),"master3":(41,120),"master4":(46,120),"master5":(51,120),"master6":(56,120),"master7":(61,120)}
        minage,maxage=ages[p.age_group]
        # Masters can register in younger age divisions; age cap is intentionally open.
        born_min=max(1900,year-maxage);born_max=min(2100,year-minage)
        if any(b not in AGE_BELTS[p.age_group][2] for b in p.belts):
            raise HTTPException(422,"Faixa não permitida para esta divisão etária no catálogo padrão")
        created=0
        for belt in dict.fromkeys(p.belts):
          for label,upper in (NO_GI_WEIGHTS if p.modality=="nogi" else GI_WEIGHTS)[p.sex]:
            name=("NO-GI" if p.modality=="nogi" else "GI")+" · "+p.age_group.upper()+" · "+("Masculino" if p.sex=="male" else "Feminino")+" · "+label
            result=await conn.execute("""
                INSERT INTO public.sport_categories
                (organizer_id,event_id,name,belt,min_weight_grams,max_weight_grams,min_birth_year,max_birth_year,sex,modality)
                VALUES($1,$2,$3,$4,0,$5,$6,$7,$8,$9)
                ON CONFLICT (event_id,sex,belt,name) DO NOTHING
            """,p.organizer_id,p.event_id,name,belt,upper,born_min,born_max,p.sex,p.modality)
            if result=="INSERT 0 1":created+=1
    return {"created":created,"requested":len(dict.fromkeys(p.belts))*len((NO_GI_WEIGHTS if p.modality=="nogi" else GI_WEIGHTS)[p.sex]),"rules":p.modality+"_weight_with_uniform","event_year":year}

async def build_default_categories(conn,oid,eid,year,modalities,absolute):
    """Adult/master preset; do not apply adult weight limits to children."""
    created=0
    for modality in modalities:
      weights=GI_WEIGHTS if modality=="gi" else NO_GI_WEIGHTS
      for group in ("adult","master1","master2","master3","master4","master5","master6","master7"):
        low,high,belts=AGE_BELTS[group]
        first=max(1900,year-high); last=min(2100,year-low)
        for sex in ("male","female"):
          for belt in belts:
            previous_max=0
            for label,maxweight in weights[sex]:
              name=f"{modality.upper()} · {group.upper()} · {'Masculino' if sex=='male' else 'Feminino'} · {label}"
              result=await conn.execute("""INSERT INTO public.sport_categories
                  (organizer_id,event_id,name,belt,min_weight_grams,max_weight_grams,min_birth_year,max_birth_year,sex,modality,is_absolute)
                  VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,false)
                  ON CONFLICT (event_id,sex,belt,name) DO NOTHING""",
                  oid,eid,name,belt,previous_max+1,maxweight,first,last,sex,modality)
              previous_max=maxweight
              created+=result=="INSERT 0 1"
            if absolute:
              name=f"{modality.upper()} · {group.upper()} · {'Masculino' if sex=='male' else 'Feminino'} · Absoluto"
              result=await conn.execute("""INSERT INTO public.sport_categories
                  (organizer_id,event_id,name,belt,min_weight_grams,max_weight_grams,min_birth_year,max_birth_year,sex,modality,is_absolute)
                  VALUES($1,$2,$3,$4,0,300000,$5,$6,$7,$8,true)
                  ON CONFLICT (event_id,sex,belt,name) DO NOTHING""",
                  oid,eid,name,belt,first,last,sex,modality)
              created+=result=="INSERT 0 1"
    return created

class CategoryUpdate(BaseModel):
    organizer_id:uuid.UUID
    name:str=Field(min_length=2,max_length=120)
    belt:str=Field(min_length=2,max_length=40)
    sex:Literal["male","female"]
    modality:Literal["gi","nogi"]
    min_weight_grams:int=Field(ge=0,le=300000)
    max_weight_grams:int=Field(ge=0,le=300000)
    min_birth_year:int=Field(ge=1900,le=2100)
    max_birth_year:int=Field(ge=1900,le=2100)

@router.patch("/categories/{category_id}")
async def update_category(category_id:uuid.UUID,p:CategoryUpdate,request:Request):
    require_origin(request)
    await authorized(request,p.organizer_id,"organizer.events.manage")
    if p.min_weight_grams>p.max_weight_grams or p.min_birth_year>p.max_birth_year:
        raise HTTPException(422,"Intervalos inválidos")
    async with request.app.state.pool.acquire() as conn:
      async with conn.transaction():
        cat=await conn.fetchrow("""SELECT c.*,e.status AS event_status FROM public.sport_categories c
          JOIN public.sport_events e ON e.id=c.event_id
          WHERE c.id=$1 AND c.organizer_id=$2 FOR UPDATE OF c""",category_id,p.organizer_id)
        if not cat: raise HTTPException(404,"Categoria não encontrada")
        if cat["event_status"] in ("closed","cancelled"):raise HTTPException(409,"Evento encerrado")
        count=await conn.fetchval("SELECT COUNT(*) FROM public.sport_entries WHERE category_id=$1",category_id)
        if count:raise HTTPException(409,"Esta categoria possui inscrições. Não é possível alterar seus critérios; crie uma nova categoria.")
        if cat["is_absolute"] and (p.min_weight_grams!=0 or p.max_weight_grams!=300000):
            raise HTTPException(422,"O absoluto precisa permanecer sem limite de peso")
        try:
            await conn.execute("""UPDATE public.sport_categories SET name=$1,belt=$2,sex=$3,modality=$4,
               min_weight_grams=$5,max_weight_grams=$6,min_birth_year=$7,max_birth_year=$8
               WHERE id=$9 AND organizer_id=$10""",p.name.strip(),p.belt.strip(),p.sex,p.modality,
               p.min_weight_grams,p.max_weight_grams,p.min_birth_year,p.max_birth_year,category_id,p.organizer_id)
        except Exception as e:
            if getattr(e,"sqlstate",None)=="23505":raise HTTPException(409,"Já existe uma categoria equivalente")
            raise
    return {"ok":True}

class CategoryDelete(BaseModel):
    organizer_id:uuid.UUID

@router.post("/categories/{category_id}/delete")
async def delete_category(category_id:uuid.UUID,p:CategoryDelete,request:Request):
    require_origin(request)
    await authorized(request,p.organizer_id,"organizer.events.manage")
    async with request.app.state.pool.acquire() as conn:
      async with conn.transaction():
        cat=await conn.fetchrow("""SELECT c.id,e.status event_status FROM public.sport_categories c
           JOIN public.sport_events e ON e.id=c.event_id
           WHERE c.id=$1 AND c.organizer_id=$2 FOR UPDATE OF c""",category_id,p.organizer_id)
        if not cat:raise HTTPException(404,"Categoria não encontrada")
        if cat["event_status"] in ("closed","cancelled"):raise HTTPException(409,"Evento encerrado")
        count=await conn.fetchval("SELECT COUNT(*) FROM public.sport_entries WHERE category_id=$1",category_id)
        if count:raise HTTPException(409,"Categoria com inscrições não pode ser excluída. Remaneje os atletas primeiro.")
        await conn.execute("DELETE FROM public.sport_categories WHERE id=$1 AND organizer_id=$2",category_id,p.organizer_id)
    return {"ok":True}

class CategorySync(BaseModel):
    organizer_id:uuid.UUID

@router.post("/events/{event_id}/sync-categories")
async def sync_event_categories(event_id:uuid.UUID,p:CategorySync,request:Request):
    require_origin(request)
    await authorized(request,p.organizer_id,"organizer.events.manage")
    async with request.app.state.pool.acquire() as conn:
      async with conn.transaction():
        event=await conn.fetchrow("""SELECT * FROM public.sport_events
            WHERE id=$1 AND organizer_id=$2 FOR UPDATE""",event_id,p.organizer_id)
        if not event:raise HTTPException(404,"Evento não encontrado")
        if event["status"] in ("closed","cancelled"):raise HTTPException(409,"Evento encerrado")
        modalities=[m for m,enabled in (("gi",event["has_gi"]),("nogi",event["has_nogi"])) if enabled]
        created=await build_default_categories(conn,p.organizer_id,event_id,event["event_date"].year,modalities,event["has_absolute"])
    return {"created":created,"message":"Categorias ausentes adicionadas sem modificar inscrições existentes"}

@router.get("/eligible-categories/{organizer_id}/{event_id}/{athlete_id}")
async def eligible_categories(organizer_id:uuid.UUID,event_id:uuid.UUID,athlete_id:uuid.UUID,request:Request):
    await authorized(request,organizer_id,"organizer.events.read")
    pool=request.app.state.pool
    athlete=await pool.fetchrow("""SELECT birth_date,belt,weight_grams,sex FROM public.sport_athletes
        WHERE id=$1 AND organizer_id=$2""",athlete_id,organizer_id)
    if not athlete:raise HTTPException(404,"Atleta não encontrado")
    rows=await pool.fetch("""SELECT c.id::text,c.name,c.belt,c.sex,c.modality,c.is_absolute,
      c.min_weight_grams,c.max_weight_grams,c.min_birth_year,c.max_birth_year
      FROM public.sport_categories c
      WHERE c.organizer_id=$1 AND c.event_id=$2 AND c.sex=$3
        AND lower(c.belt)=lower($4)
        AND $5::int BETWEEN c.min_birth_year AND c.max_birth_year
        AND $6::int BETWEEN c.min_weight_grams AND c.max_weight_grams
        AND NOT EXISTS(SELECT 1 FROM public.sport_entries e WHERE e.athlete_id=$7 AND e.category_id=c.id AND e.payment_status<>'cancelled')
      ORDER BY c.modality,c.is_absolute,c.max_weight_grams,c.name LIMIT 500
    """,organizer_id,event_id,athlete["sex"],athlete["belt"],athlete["birth_date"].year,athlete["weight_grams"],athlete_id)
    return {"items":[dict(x) for x in rows]}

class EntryChange(BaseModel):
    organizer_id:uuid.UUID
    category_id:uuid.UUID
    base_cents:int=Field(ge=0,le=100000000)

class EntryCancel(BaseModel):
    organizer_id:uuid.UUID

@router.patch("/entries/{entry_id}")
async def edit_entry(entry_id:uuid.UUID,p:EntryChange,request:Request):
    require_origin(request)
    user=await authorized(request,p.organizer_id,"organizer.registrations.manage")
    async with request.app.state.pool.acquire() as conn:
      async with conn.transaction():
        entry=await conn.fetchrow("""SELECT e.*,v.status event_status FROM public.sport_entries e
          JOIN public.sport_events v ON v.id=e.event_id
          WHERE e.id=$1 AND e.organizer_id=$2 FOR UPDATE OF e""",entry_id,p.organizer_id)
        if not entry:raise HTTPException(404,"Inscrição não encontrada")
        closed=await conn.fetchval("SELECT registration_closed_at IS NOT NULL OR (registration_deadline IS NOT NULL AND now()>=registration_deadline) FROM public.sport_events WHERE id=$1",entry["event_id"])
        if closed:raise HTTPException(409,"Inscrições encerradas; alterações bloqueadas")
        if entry["payment_status"]!="pending":raise HTTPException(409,"Inscrição com pagamento confirmado ou cancelada exige revisão financeira; edição bloqueada")
        if entry["event_status"] in ("closed","cancelled"):raise HTTPException(409,"Campeonato encerrado")
        athlete=await conn.fetchrow("SELECT birth_date,belt,weight_grams,sex FROM public.sport_athletes WHERE id=$1 AND organizer_id=$2",entry["athlete_id"],p.organizer_id)
        cat=await conn.fetchrow("""SELECT belt,sex,min_weight_grams,max_weight_grams,min_birth_year,max_birth_year
          FROM public.sport_categories WHERE id=$1 AND event_id=$2 AND organizer_id=$3""",p.category_id,entry["event_id"],p.organizer_id)
        if not cat:raise HTTPException(404,"Categoria não encontrada neste evento")
        if not athlete or not athlete["sex"] or athlete["sex"]!=cat["sex"] or athlete["belt"].casefold()!=cat["belt"].casefold() or not(cat["min_weight_grams"]<=athlete["weight_grams"]<=cat["max_weight_grams"]) or not(cat["min_birth_year"]<=athlete["birth_date"].year<=cat["max_birth_year"]):
            raise HTTPException(422,"Categoria incompatível com sexo, faixa, idade ou peso do atleta")
        try:
            await conn.execute("UPDATE public.sport_entries SET category_id=$1,base_cents=$2 WHERE id=$3 AND organizer_id=$4",p.category_id,p.base_cents,entry_id,p.organizer_id)
        except Exception as e:
            if getattr(e,"sqlstate",None)=="23505":raise HTTPException(409,"Atleta já possui inscrição nesta categoria")
            raise
        await conn.execute("""INSERT INTO public.finance_audit(organizer_id,actor_user_id,action,reference_id)
          VALUES($1,$2,$3,$4)""",p.organizer_id,user.user_id,
          "sport_entry_edited:"+str(entry["category_id"])+":"+str(entry["base_cents"])+"->"+str(p.category_id)+":"+str(p.base_cents),entry_id)
    return {"ok":True}

@router.post("/entries/{entry_id}/cancel")
async def cancel_entry(entry_id:uuid.UUID,p:EntryCancel,request:Request):
    require_origin(request)
    user=await authorized(request,p.organizer_id,"organizer.registrations.manage")
    async with request.app.state.pool.acquire() as conn:
      async with conn.transaction():
        entry=await conn.fetchrow("SELECT payment_status FROM public.sport_entries WHERE id=$1 AND organizer_id=$2 FOR UPDATE",entry_id,p.organizer_id)
        if not entry:raise HTTPException(404,"Inscrição não encontrada")
        if await conn.fetchval("SELECT registration_closed_at IS NOT NULL OR (registration_deadline IS NOT NULL AND now()>=registration_deadline) FROM public.sport_events WHERE id=(SELECT event_id FROM public.sport_entries WHERE id=$1)",entry_id):
            raise HTTPException(409,"Inscrições encerradas; cancelamento exige revisão administrativa")
        if entry["payment_status"]=="cancelled":raise HTTPException(409,"Inscrição já cancelada")
        if entry["payment_status"]!="pending":raise HTTPException(409,"Pagamento já confirmado. É necessária revisão financeira antes do cancelamento")
        await conn.execute("""UPDATE public.sport_entries SET payment_status='cancelled',eligible=false WHERE id=$1 AND organizer_id=$2""",entry_id,p.organizer_id)
        await conn.execute("""INSERT INTO public.finance_audit(organizer_id,actor_user_id,action,reference_id)
          VALUES($1,$2,'sport_entry_cancelled', $3)""",p.organizer_id,user.user_id,entry_id)
    return {"ok":True}

class BracketAction(BaseModel):
    organizer_id:uuid.UUID

@router.post("/events/{event_id}/close-registrations")
async def close_registrations(event_id:uuid.UUID,p:BracketAction,request:Request):
    require_origin(request)
    user=await authorized(request,p.organizer_id,"organizer.events.manage")
    async with request.app.state.pool.acquire() as conn:
      async with conn.transaction():
        event=await conn.fetchrow("SELECT status,registration_closed_at FROM public.sport_events WHERE id=$1 AND organizer_id=$2 FOR UPDATE",event_id,p.organizer_id)
        if not event:raise HTTPException(404,"Evento não encontrado")
        if event["status"] in ("closed","cancelled"):raise HTTPException(409,"Evento indisponível")
        if event["registration_closed_at"]:return {"ok":True,"already_closed":True}
        await conn.execute("UPDATE public.sport_events SET registration_closed_at=now() WHERE id=$1",event_id)
        await conn.execute("INSERT INTO public.finance_audit(organizer_id,actor_user_id,action,reference_id) VALUES($1,$2,'sport_registrations_closed',$3)",p.organizer_id,user.user_id,event_id)
    return {"ok":True}

@router.get("/events/{event_id}/bracket-preview/{organizer_id}")
async def bracket_preview(event_id:uuid.UUID,organizer_id:uuid.UUID,request:Request):
    await authorized(request,organizer_id,"organizer.events.read")
    pool=request.app.state.pool
    event=await pool.fetchrow("SELECT registration_closed_at,registration_deadline FROM public.sport_events WHERE id=$1 AND organizer_id=$2",event_id,organizer_id)
    if not event:raise HTTPException(404,"Evento não encontrado")
    groups=await pool.fetch("""SELECT c.id::text category_id,c.name,c.belt,c.modality,COUNT(e.id)::int participants
      FROM public.sport_categories c LEFT JOIN public.sport_entries e
      ON e.category_id=c.id AND e.event_id=$1 AND e.eligible=true AND e.payment_status IN ('external_confirmed','online_confirmed','complimentary')
      WHERE c.event_id=$1 AND c.organizer_id=$2 GROUP BY c.id ORDER BY participants DESC,c.name LIMIT 1000""",event_id,organizer_id)
    saved=await pool.fetch("SELECT category_id::text,status FROM public.sport_brackets WHERE event_id=$1 AND organizer_id=$2",event_id,organizer_id)
    return {"closed":bool(event["registration_closed_at"]) or bool(event["registration_deadline"] and event["registration_deadline"]<=datetime.now(event["registration_deadline"].tzinfo)),"categories":[dict(g) for g in groups if g["participants"]>0],"brackets":[dict(x) for x in saved]}

@router.post("/events/{event_id}/generate-brackets")
async def generate_brackets(event_id:uuid.UUID,p:BracketAction,request:Request):
    require_origin(request)
    user=await authorized(request,p.organizer_id,"organizer.events.manage")
    count=0;matches=0
    async with request.app.state.pool.acquire() as conn:
      async with conn.transaction():
        event=await conn.fetchrow("SELECT registration_closed_at,registration_deadline,payment_deadline,status FROM public.sport_events WHERE id=$1 AND organizer_id=$2 FOR UPDATE",event_id,p.organizer_id)
        if not event:raise HTTPException(404,"Evento não encontrado")
        if event["status"] in ("closed","cancelled"):raise HTTPException(409,"Evento indisponível")
        if not (event["registration_closed_at"] or (event["registration_deadline"] and event["registration_deadline"]<=datetime.now(event["registration_deadline"].tzinfo))):raise HTTPException(409,"Encerre as inscrições antes de gerar chaves")
        if event["payment_deadline"] and event["payment_deadline"]>datetime.now(event["payment_deadline"].tzinfo):raise HTTPException(409,"Aguarde o encerramento do prazo de pagamento para gerar chaves")
        if await conn.fetchval("SELECT COUNT(*) FROM public.sport_brackets WHERE event_id=$1",event_id):raise HTTPException(409,"Chaves já geradas. Não serão sobrescritas automaticamente")
        rows=await conn.fetch("""SELECT e.id entry_id,e.category_id,a.team FROM public.sport_entries e
           JOIN public.sport_athletes a ON a.id=e.athlete_id WHERE e.event_id=$1 AND e.organizer_id=$2 AND e.eligible=true
           AND e.payment_status IN ('external_confirmed','online_confirmed','complimentary')
           ORDER BY e.category_id,a.team,e.id""",event_id,p.organizer_id)
        grouped={}
        for r in rows:grouped.setdefault(r["category_id"],[]).append(r)
        for category,athletes in grouped.items():
            if len(athletes)<2:continue
            # Spread teammates across opening-round pairings whenever possible.
            pools={}
            for athlete in athletes:pools.setdefault((athlete["team"] or "").strip().lower(),[]).append(athlete)
            ordered=[]
            while any(pools.values()):
                for team in sorted(pools,key=lambda k:len(pools[k]),reverse=True):
                    if pools[team]:ordered.append(pools[team].pop(0))
            size=1<<(len(ordered)-1).bit_length()
            byes=size-len(ordered)
            # Seed BYEs as distributed pairs, not only at the tail of the draw.
            slots=[None]*size
            bye_pairs=set()
            if byes:
                for j in range(byes):
                    bye_pairs.add((j*(size//2//byes)) % (size//2))
            pairs=list(range(size//2))
            # Fill an automatic advancement slot in each designated pair.
            for pair in pairs:
                if pair in bye_pairs:
                    slots[2*pair]=ordered.pop(0)["entry_id"]
                else:
                    slots[2*pair]=ordered.pop(0)["entry_id"]
                    slots[2*pair+1]=ordered.pop(0)["entry_id"]
            bracket=await conn.fetchval("""INSERT INTO public.sport_brackets(organizer_id,event_id,category_id,created_by)
               VALUES($1,$2,$3,$4) RETURNING id""",p.organizer_id,event_id,category,user.user_id)
            for rnd in range(1,size.bit_length()):
                for idx in range(size//(2**rnd)):
                    red=slots[2*idx] if rnd==1 else None
                    blue=slots[2*idx+1] if rnd==1 else None
                    is_bye=rnd==1 and ((red is None)!=(blue is None))
                    winner=(red or blue) if is_bye else None
                    await conn.execute("""INSERT INTO public.sport_bracket_matches
                       (bracket_id,round_number,position,red_entry_id,blue_entry_id,status,winner_entry_id)
                       VALUES($1,$2,$3,$4,$5,$6,$7)""",bracket,rnd,idx+1,red,blue,"bye" if is_bye else "scheduled",winner)
                    matches+=1
            # Pass BYE winners to the corresponding next-round slot.
            if size>2:
                opening=await conn.fetch("""SELECT position,winner_entry_id FROM public.sport_bracket_matches
                   WHERE bracket_id=$1 AND round_number=1 AND status='bye'""",bracket)
                for item in opening:
                    next_position=(item["position"]+1)//2
                    field="red_entry_id" if item["position"]%2 else "blue_entry_id"
                    await conn.execute("UPDATE public.sport_bracket_matches SET "+field+"=$1 WHERE bracket_id=$2 AND round_number=2 AND position=$3",
                      item["winner_entry_id"],bracket,next_position)
            count+=1
        await conn.execute("INSERT INTO public.finance_audit(organizer_id,actor_user_id,action,reference_id) VALUES($1,$2,'sport_brackets_generated',$3)",p.organizer_id,user.user_id,event_id)
    return {"brackets_created":count,"matches_created":matches,"status":"draft"}

@router.post("/events/{event_id}/publish-brackets")
async def publish_brackets(event_id:uuid.UUID,p:BracketAction,request:Request):
    require_origin(request)
    user=await authorized(request,p.organizer_id,"organizer.events.manage")
    async with request.app.state.pool.acquire() as conn:
      async with conn.transaction():
        event=await conn.fetchrow("SELECT registration_closed_at,registration_deadline FROM public.sport_events WHERE id=$1 AND organizer_id=$2 FOR UPDATE",event_id,p.organizer_id)
        if not event:raise HTTPException(404,"Evento não encontrado")
        if not (event["registration_closed_at"] or (event["registration_deadline"] and event["registration_deadline"]<=datetime.now(event["registration_deadline"].tzinfo))):raise HTTPException(409,"Inscrições não encerradas")
        n=await conn.fetchval("SELECT count(*) FROM public.sport_brackets WHERE event_id=$1 AND organizer_id=$2",event_id,p.organizer_id)
        if not n:raise HTTPException(409,"Gere e confira as chaves primeiro")
        await conn.execute("UPDATE public.sport_brackets SET status='published',published_at=COALESCE(published_at,now()) WHERE event_id=$1 AND organizer_id=$2 AND status='draft'",event_id,p.organizer_id)
        await conn.execute("INSERT INTO public.finance_audit(organizer_id,actor_user_id,action,reference_id) VALUES($1,$2,'sport_brackets_published',$3)",p.organizer_id,user.user_id,event_id)
    return {"ok":True,"published_brackets":n}

@router.get("/events/{event_id}/brackets/{organizer_id}")
async def view_brackets(event_id:uuid.UUID,organizer_id:uuid.UUID,request:Request):
    await authorized(request,organizer_id,"organizer.events.read")
    rows=await request.app.state.pool.fetch("""SELECT b.id::text bracket_id,b.status bracket_status,
       c.name category,c.belt,c.modality,m.round_number,m.position,m.status match_status,
       m.id::text match_id,m.red_entry_id::text red_entry_id,m.blue_entry_id::text blue_entry_id,m.winner_entry_id::text winner_entry_id,m.result_method,m.result_notes,m.completed_at,ra.team red_team,ba.team blue_team,
       ra.full_name red_name,ba.full_name blue_name
       FROM public.sport_brackets b JOIN public.sport_categories c ON c.id=b.category_id
       JOIN public.sport_bracket_matches m ON m.bracket_id=b.id
       LEFT JOIN public.sport_entries re ON re.id=m.red_entry_id
       LEFT JOIN public.sport_athletes ra ON ra.id=re.athlete_id
       LEFT JOIN public.sport_entries be ON be.id=m.blue_entry_id
       LEFT JOIN public.sport_athletes ba ON ba.id=be.athlete_id
       WHERE b.event_id=$1 AND b.organizer_id=$2 ORDER BY c.name,c.belt,b.id,m.round_number,m.position
       LIMIT 3000""",event_id,organizer_id)
    return {"items":[dict(x) for x in rows]}

@router.get("/public/events/{event_id}/participants")
async def public_participants(event_id:uuid.UUID,request:Request):
    # Public output intentionally excludes bracket structure, payment and private athlete data.
    event=await request.app.state.pool.fetchrow("""SELECT id::text,name,event_date,city
        FROM public.sport_events WHERE id=$1 AND public_roster_enabled=true AND status<>'cancelled'""",event_id)
    if not event:raise HTTPException(404,"Evento não está disponível para consulta pública")
    rows=await request.app.state.pool.fetch("""SELECT a.full_name,c.name category,c.belt,c.modality,c.sex,e.payment_status,e.eligible
      FROM public.sport_entries e JOIN public.sport_athletes a ON a.id=e.athlete_id
      JOIN public.sport_categories c ON c.id=e.category_id
      WHERE e.event_id=$1 AND e.payment_status <> 'cancelled'
      ORDER BY c.modality,c.name,c.belt,a.full_name LIMIT 5000""",event_id)
    return {"event":dict(event),"participants":[dict(r) for r in rows]}

@router.get("/public/events")
async def public_events(request:Request):
    rows=await request.app.state.pool.fetch("""SELECT id::text,name,event_date,city FROM public.sport_events
       WHERE public_roster_enabled=true AND status<>'cancelled' ORDER BY event_date DESC LIMIT 100""")
    return {"items":[dict(r) for r in rows]}

class PublicRosterSetting(BaseModel):
    organizer_id:uuid.UUID
    enabled:bool

@router.post("/events/{event_id}/public-roster")
async def set_public_roster(event_id:uuid.UUID,p:PublicRosterSetting,request:Request):
    require_origin(request)
    user=await authorized(request,p.organizer_id,"organizer.events.manage")
    async with request.app.state.pool.acquire() as conn:
      async with conn.transaction():
        row=await conn.fetchrow("""UPDATE public.sport_events SET public_roster_enabled=$1
            WHERE id=$2 AND organizer_id=$3 AND status<>'cancelled' RETURNING id""",p.enabled,event_id,p.organizer_id)
        if not row:raise HTTPException(404,"Evento indisponível")
        await conn.execute("""INSERT INTO public.finance_audit(organizer_id,actor_user_id,action,reference_id)
           VALUES($1,$2,$3,$4)""",p.organizer_id,user.user_id,"sport_public_roster_enabled" if p.enabled else "sport_public_roster_disabled",event_id)
    return {"ok":True,"enabled":p.enabled}

class BracketSwap(BaseModel):
    organizer_id:uuid.UUID
    first_match_id:uuid.UUID
    first_side:Literal["red","blue"]
    second_match_id:uuid.UUID
    second_side:Literal["red","blue"]

@router.post("/brackets/{bracket_id}/swap")
async def swap_bracket_slots(bracket_id:uuid.UUID,p:BracketSwap,request:Request):
    require_origin(request)
    user=await authorized(request,p.organizer_id,"organizer.events.manage")
    if p.first_match_id==p.second_match_id and p.first_side==p.second_side:
        raise HTTPException(422,"Escolha duas posições diferentes")
    async with request.app.state.pool.acquire() as conn:
      async with conn.transaction():
        bracket=await conn.fetchrow("""SELECT id,event_id,status FROM public.sport_brackets
            WHERE id=$1 AND organizer_id=$2 FOR UPDATE""",bracket_id,p.organizer_id)
        if not bracket:raise HTTPException(404,"Chave não encontrada")
        if bracket["status"]!="draft":raise HTTPException(409,"Chave publicada: alterações bloqueadas")
        matches=await conn.fetch("""SELECT id,round_number,red_entry_id,blue_entry_id
            FROM public.sport_bracket_matches WHERE bracket_id=$1 AND id=ANY($2::uuid[])
            ORDER BY id FOR UPDATE""",bracket_id,[p.first_match_id,p.second_match_id])
        if len(matches)!=(1 if p.first_match_id==p.second_match_id else 2) or any(m["round_number"]!=1 for m in matches):
            raise HTTPException(422,"Só é possível trocar atletas na primeira rodada da mesma chave")
        lookup={m["id"]:m for m in matches}
        a=lookup[p.first_match_id][p.first_side+"_entry_id"]
        b=lookup[p.second_match_id][p.second_side+"_entry_id"]
        if a is None or b is None:raise HTTPException(422,"Selecione duas posições ocupadas")
        # Swapping participants cannot create a second entry or alter payment records.
        updates=[(p.first_match_id,p.first_side,b),(p.second_match_id,p.second_side,a)]
        for match_id,side,value in updates:
            await conn.execute("UPDATE public.sport_bracket_matches SET "+side+"_entry_id=$1 WHERE id=$2",value,match_id)
        # Recalculate automatic BYEs and their seeded advancement slots.
        opening=await conn.fetch("""SELECT position,red_entry_id,blue_entry_id FROM public.sport_bracket_matches
            WHERE bracket_id=$1 AND round_number=1 ORDER BY position""",bracket_id)
        for m in opening:
            is_bye=(m["red_entry_id"] is None)!=(m["blue_entry_id"] is None)
            winner=(m["red_entry_id"] or m["blue_entry_id"]) if is_bye else None
            await conn.execute("""UPDATE public.sport_bracket_matches SET status=$1,winner_entry_id=$2
                WHERE bracket_id=$3 AND round_number=1 AND position=$4""",
                "bye" if is_bye else "scheduled",winner,bracket_id,m["position"])
            next_position=(m["position"]+1)//2
            side="red" if m["position"]%2 else "blue"
            await conn.execute("UPDATE public.sport_bracket_matches SET "+side+"_entry_id=$1 WHERE bracket_id=$2 AND round_number=2 AND position=$3",
                winner,bracket_id,next_position)
        await conn.execute("""INSERT INTO public.finance_audit(organizer_id,actor_user_id,action,reference_id,details)
            VALUES($1,$2,'sport_bracket_slots_swapped',$3,$4)""",
            p.organizer_id,user.user_id,bracket_id,str(p.first_match_id)+":"+p.first_side+" <> "+str(p.second_match_id)+":"+p.second_side)
    return {"ok":True}

class MatchResult(BaseModel):
    organizer_id:uuid.UUID
    winner_entry_id:uuid.UUID
    method:Literal["points","submission","decision","disqualification","wo"]
    notes:str=Field(default="",max_length=500)

@router.post("/brackets/matches/{match_id}/result")
async def register_match_result(match_id:uuid.UUID,p:MatchResult,request:Request):
    require_origin(request)
    user=await authorized(request,p.organizer_id,"organizer.events.manage")
    async with request.app.state.pool.acquire() as conn:
      async with conn.transaction():
        match=await conn.fetchrow("""SELECT m.*,b.event_id,b.category_id,b.status bracket_status
            FROM public.sport_bracket_matches m JOIN public.sport_brackets b ON b.id=m.bracket_id
            WHERE m.id=$1 AND b.organizer_id=$2 FOR UPDATE OF m,b""",match_id,p.organizer_id)
        if not match:raise HTTPException(404,"Luta não encontrada")
        if match["bracket_status"]!="published":raise HTTPException(409,"Publique a chave antes de registrar resultados")
        if match["status"]!="scheduled":raise HTTPException(409,"Luta já concluída ou resolvida por BYE")
        if not match["red_entry_id"] or not match["blue_entry_id"]:
            raise HTTPException(409,"Aguarde a definição dos dois atletas")
        if p.winner_entry_id not in (match["red_entry_id"],match["blue_entry_id"]):
            raise HTTPException(422,"Vencedor não pertence a este confronto")
        # Never overwrite an advanced winner or a previously completed fight.
        nxt=await conn.fetchrow("""SELECT id,red_entry_id,blue_entry_id,status
            FROM public.sport_bracket_matches WHERE bracket_id=$1 AND round_number=$2 AND position=$3 FOR UPDATE""",
            match["bracket_id"],match["round_number"]+1,(match["position"]+1)//2)
        if nxt:
            target="red_entry_id" if match["position"]%2 else "blue_entry_id"
            if nxt[target] is not None:raise HTTPException(409,"Destino já ocupado; revisão administrativa necessária")
            if nxt["status"]!="scheduled":raise HTTPException(409,"A próxima luta já foi concluída")
        await conn.execute("""UPDATE public.sport_bracket_matches SET status=$1,winner_entry_id=$2,
            result_method=$3,result_notes=$4,completed_at=now(),completed_by=$5 WHERE id=$6""",
            "wo" if p.method=="wo" else "finished",p.winner_entry_id,p.method,p.notes.strip(),user.user_id,match_id)
        if nxt:
            await conn.execute("UPDATE public.sport_bracket_matches SET "+target+"=$1 WHERE id=$2",p.winner_entry_id,nxt["id"])
        await conn.execute("""INSERT INTO public.finance_audit(organizer_id,actor_user_id,action,reference_id,details)
            VALUES($1,$2,'sport_match_result_registered',$3,$4)""",
            p.organizer_id,user.user_id,match_id,p.method+":"+str(p.winner_entry_id)+":"+p.notes.strip())
    return {"ok":True,"winner_entry_id":str(p.winner_entry_id),"method":p.method,
            "champion":nxt is None}

