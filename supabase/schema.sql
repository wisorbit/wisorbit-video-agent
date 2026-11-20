-- WisOrbit AI Video Agent - database schema.
-- Safe to run again and again in the Supabase SQL editor (everything is "if not exists" / "or replace").

-- ============ Phase 1: profiles + video history ============
create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text,
  plan text not null default 'free',
  credits int not null default 0,
  created_at timestamptz not null default now()
);
create table if not exists public.videos (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  topic text,
  lang text,
  style text,
  duration_sec int,
  status text not null default 'queued',
  storage_path text,
  created_at timestamptz not null default now()
);
create index if not exists videos_user_created on public.videos(user_id, created_at desc);

alter table public.profiles enable row level security;
alter table public.videos enable row level security;
drop policy if exists "own profile" on public.profiles;
create policy "own profile" on public.profiles for select to authenticated using (auth.uid() = id);
drop policy if exists "own videos" on public.videos;
create policy "own videos" on public.videos for select to authenticated using (auth.uid() = user_id);

-- auto-create profile on signup
create or replace function public.handle_new_user() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles(id, email) values (new.id, new.email) on conflict (id) do nothing;
  return new;
end $$;
drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created after insert on auth.users
  for each row execute function public.handle_new_user();

-- private storage bucket; files stored as <user_id>/<file>
insert into storage.buckets (id, name, public) values ('videos', 'videos', false)
  on conflict (id) do nothing;
drop policy if exists "own video files" on storage.objects;
create policy "own video files" on storage.objects for select to authenticated
  using (bucket_id = 'videos' and (storage.foldername(name))[1] = auth.uid()::text);

-- ============ Phase 2: render job queue ============
-- Users submit through the web app (server key); any number of workers (Render, Oracle VM, ...) claim jobs.
create table if not exists public.jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  video_id uuid references public.videos(id) on delete cascade,
  topic text not null,
  minutes numeric(4,1) not null default 1,
  style text not null default 'educational',
  langs text not null default 'hi,en',
  plan text not null default 'free',          -- plan at submit time (free / pro / premium)
  priority int not null default 0,            -- higher = served first (paid plans later)
  status text not null default 'queued'
    check (status in ('queued','running','done','error','cancelled')),
  step text,
  progress int not null default 0,
  worker_id text,
  attempts int not null default 0,
  files text,                                 -- comma-separated storage paths
  error text,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  heartbeat_at timestamptz
);
create index if not exists jobs_queue on public.jobs(priority desc, created_at) where status = 'queued';
create index if not exists jobs_user_created on public.jobs(user_id, created_at desc);
-- one active (queued/running) job per user, enforced by the database itself
create unique index if not exists jobs_one_active_per_user on public.jobs(user_id) where status in ('queued','running');

create table if not exists public.workers (
  id text primary key,
  host text,
  started_at timestamptz not null default now(),
  last_seen timestamptz not null default now(),
  current_job uuid,
  jobs_done int not null default 0
);

alter table public.jobs enable row level security;
alter table public.workers enable row level security;
drop policy if exists "own jobs" on public.jobs;
create policy "own jobs" on public.jobs for select to authenticated using (auth.uid() = user_id);
-- workers: no policies -> only the server key can read/write

-- Atomically take the next job (highest priority, then oldest). Safe with many workers.
create or replace function public.claim_job(p_worker text) returns setof public.jobs
language plpgsql set search_path = public as $$
declare v_id uuid;
begin
  if current_user not in ('service_role', 'postgres', 'supabase_admin') then raise exception 'not allowed'; end if;
  select id into v_id from public.jobs
   where status = 'queued'
   order by priority desc, created_at
   limit 1
   for update skip locked;
  if v_id is null then return; end if;
  update public.workers set current_job = v_id, last_seen = now() where id = p_worker;
  return query
    update public.jobs
       set status = 'running', worker_id = p_worker, started_at = now(), heartbeat_at = now(),
           attempts = attempts + 1, step = 'starting', progress = 0
     where id = v_id
    returning *;
end $$;

-- Jobs whose worker died (no heartbeat) go back to the queue; after 2 tries they fail.
create or replace function public.reap_stale_jobs(p_timeout_sec int default 300) returns int
language plpgsql set search_path = public as $$
declare n int;
begin
  if current_user not in ('service_role', 'postgres', 'supabase_admin') then raise exception 'not allowed'; end if;
  with r as (
    update public.jobs
       set status = case when attempts >= 2 then 'error' else 'queued' end,
           step = case when attempts >= 2 then 'worker stopped' else 'requeued' end,
           error = case when attempts >= 2 then 'worker stopped twice' else error end,
           worker_id = null,
           finished_at = case when attempts >= 2 then now() else null end
     where status = 'running' and heartbeat_at < now() - make_interval(secs => p_timeout_sec)
    returning video_id, status
  ), v as (
    update public.videos vv set status = r.status from r where vv.id = r.video_id returning 1
  )
  select count(*) into n from r;
  return n;
end $$;

-- Queue position + numbers for the wait estimate.
create or replace function public.job_queue_info(p_job uuid) returns json
language plpgsql stable set search_path = public as $$
declare res json;
begin
  if current_user not in ('service_role', 'postgres', 'supabase_admin') then raise exception 'not allowed'; end if;
  with me as (select * from public.jobs where id = p_job),
  ahead as (
    select j.* from public.jobs j, me
     where j.status = 'queued' and j.id <> me.id
       and (j.priority > me.priority or (j.priority = me.priority and j.created_at < me.created_at))
  ),
  running as (select * from public.jobs where status = 'running'),
  recent as (
    select extract(epoch from (finished_at - started_at))
           / greatest(minutes * (case when langs like '%,%' then 2 else 1 end), 0.5) as spm
      from public.jobs
     where status = 'done' and started_at is not null and finished_at is not null
     order by finished_at desc limit 20
  )
  select json_build_object(
    'status', (select status from me),
    'position', case when (select status from me) = 'queued' then (select count(*) from ahead) + 1 else 0 end,
    'ahead_work', coalesce((select sum(minutes * (case when langs like '%,%' then 2 else 1 end)) from ahead), 0),
    'running_left', coalesce((select sum(minutes * (case when langs like '%,%' then 2 else 1 end) * (100 - progress) / 100.0) from running), 0),
    'running', (select count(*) from running),
    'workers', (select count(*) from public.workers where last_seen > now() - interval '3 minutes'),
    'sec_per_min', (select percentile_cont(0.5) within group (order by spm) from recent)
  ) into res;
  return res;
end $$;

-- (functions also refuse anyone but the server key, even if Supabase default privileges grant EXECUTE)
-- ============ Table/function grants ============
-- New Supabase projects no longer grant table access to the API roles automatically.
grant usage on schema public to anon, authenticated, service_role;
grant select, insert, update, delete on table public.profiles, public.videos, public.jobs, public.workers to service_role;
grant select on table public.profiles, public.videos, public.jobs to authenticated;
revoke all on function public.claim_job(text) from public, anon, authenticated;
revoke all on function public.reap_stale_jobs(int) from public, anon, authenticated;
revoke all on function public.job_queue_info(uuid) from public, anon, authenticated;
grant execute on function public.claim_job(text), public.reap_stale_jobs(int), public.job_queue_info(uuid) to service_role;

-- ============ Phase 3: plans + payments (Razorpay prepaid passes) ============
-- (same text as supabase/migrations/phase3_plans_payments.sql - run that file alone on the live DB)
-- Safe to run again and again. Prices/limits live in public.plans: edit them in Supabase Table Editor,
-- the website picks the change up within 60 seconds. Seed rows are inserted only if missing (your edits are kept).

create table if not exists public.plans (
  id text primary key,                         -- slug: free, starter, pro, creator, pack, pro_yearly
  kind text not null default 'pass' check (kind in ('free','pass','pack')),
  name_hi text not null,
  name_en text not null,
  desc_hi text,
  desc_en text,
  price_paise int not null default 0 check (price_paise >= 0),   -- in paise: Rs 199 = 19900
  period_days int check (period_days is null or period_days > 0), -- 30 / 365; null = one-time pack
  minutes_quota int not null default 0,        -- video minutes per 30 days (pack: minutes added once)
  daily_videos int,                            -- videos per day (IST) - free plan; null = no daily cap
  max_video_minutes numeric(4,1) not null default 1,
  resolution int not null default 720 check (resolution in (720, 1080)),
  styles text[],                               -- allowed styles; null = all styles
  both_langs_one_charge boolean not null default false,  -- Hindi+English together counted once
  priority int not null default 0,             -- queue priority (higher = first)
  active boolean not null default true,        -- false = hidden from the plans page / not for sale
  sort int not null default 0,
  updated_at timestamptz not null default now()
);

insert into public.plans (id, kind, name_hi, name_en, desc_hi, desc_en, price_paise, period_days, minutes_quota,
                          daily_videos, max_video_minutes, resolution, styles, both_langs_one_charge, priority, sort) values
 ('free', 'free', 'मुफ़्त', 'Free', 'रोज़ 2 वीडियो, हर वीडियो 1 मिनट तक', '2 videos a day, up to 1 min each',
   0, null, 0, 2, 1, 720, '{educational,youtube}', false, 0, 0),
 ('starter', 'pass', 'स्टार्टर', 'Starter', '30 दिन में 60 मिनट, 5 मिनट तक की वीडियो', '60 min in 30 days, videos up to 5 min',
   19900, 30, 60, null, 5, 1080, '{educational,youtube}', false, 5, 10),
 ('pro', 'pass', 'प्रो', 'Pro', '30 दिन में 200 मिनट, 10 मिनट तक, सभी स्टाइल, जल्दी नंबर', '200 min in 30 days, up to 10 min, all styles, priority',
   49900, 30, 200, null, 10, 1080, null, false, 10, 20),
 ('creator', 'pass', 'क्रिएटर', 'Creator', '30 दिन में 600 मिनट, हिंदी+English एक साथ, सबसे पहले नंबर', '600 min in 30 days, Hindi+English together, top priority',
   129900, 30, 600, null, 10, 1080, null, true, 20, 30),
 ('pack', 'pack', 'मिनट पैक', 'Minute pack', '15 मिनट एक बार — प्लान की तारीख़ नहीं बदलती', '15 minutes once — does not change your plan dates',
   4900, null, 15, null, 5, 1080, '{educational,youtube}', false, 5, 40),
 ('pro_yearly', 'pass', 'प्रो सालाना', 'Pro yearly', 'हर महीने 200 मिनट, 12 महीने (2 महीने मुफ़्त)', '200 min every month for 12 months (2 months free)',
   499000, 365, 200, null, 10, 1080, null, false, 10, 50)
on conflict (id) do nothing;

-- per-user entitlement
alter table public.profiles add column if not exists plan_expires_at timestamptz;      -- null for free
alter table public.profiles add column if not exists minutes_left numeric(8,1) not null default 0;   -- this period
alter table public.profiles add column if not exists bonus_minutes numeric(8,1) not null default 0;  -- packs, no expiry
alter table public.profiles add column if not exists period_resets_at timestamptz;     -- next monthly refill

-- what each job cost (for refunds when a video fails or is cancelled)
alter table public.jobs add column if not exists resolution int not null default 720;
alter table public.jobs add column if not exists cost_minutes numeric(6,1) not null default 0;
alter table public.jobs add column if not exists from_plan numeric(6,1) not null default 0;
alter table public.jobs add column if not exists from_bonus numeric(6,1) not null default 0;
alter table public.jobs add column if not exists refunded boolean not null default false;

-- payments audit + idempotency
create table if not exists public.orders (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete set null,
  email text,
  plan_id text not null,
  amount_paise int not null,
  currency text not null default 'INR',
  razorpay_order_id text unique,
  razorpay_payment_id text unique,
  status text not null default 'created' check (status in ('created','paid','failed')),
  paid_via text,                               -- verify / webhook
  error text,
  created_at timestamptz not null default now(),
  paid_at timestamptz
);
create index if not exists orders_user_created on public.orders(user_id, created_at desc);

alter table public.plans enable row level security;
alter table public.orders enable row level security;
drop policy if exists "public read active plans" on public.plans;
create policy "public read active plans" on public.plans for select to anon, authenticated using (active);
drop policy if exists "own orders" on public.orders;
create policy "own orders" on public.orders for select to authenticated using (auth.uid() = user_id);

-- Mark an order paid and give the plan in ONE transaction. Idempotent: a second call (duplicate webhook,
-- verify + webhook) returns 'already'. The new profile values are computed by the app (billing.py) from
-- the values it read; if the profile changed meanwhile this returns 'retry' and the app recomputes.
create or replace function public.commit_activation(
  p_order uuid, p_payment text, p_via text,
  p_expect jsonb,   -- {"plan","plan_expires_at","minutes_left","bonus_minutes"} as read by the app
  p_new jsonb       -- {"plan","plan_expires_at","minutes_left","bonus_minutes","period_resets_at"}
) returns text
language plpgsql set search_path = public as $$
declare o public.orders; p public.profiles;
begin
  if current_user not in ('service_role', 'postgres', 'supabase_admin') then raise exception 'not allowed'; end if;
  select * into o from public.orders where id = p_order for update;
  if not found then return 'no_order'; end if;
  if o.status = 'paid' then return 'already'; end if;
  select * into p from public.profiles where id = o.user_id for update;
  if not found then return 'no_profile'; end if;
  if p.plan is distinct from (p_expect->>'plan')
     or p.plan_expires_at is distinct from (p_expect->>'plan_expires_at')::timestamptz
     or p.minutes_left is distinct from (p_expect->>'minutes_left')::numeric
     or p.bonus_minutes is distinct from (p_expect->>'bonus_minutes')::numeric then
    return 'retry';
  end if;
  update public.profiles set
    plan = p_new->>'plan',
    plan_expires_at = (p_new->>'plan_expires_at')::timestamptz,
    minutes_left = (p_new->>'minutes_left')::numeric,
    bonus_minutes = (p_new->>'bonus_minutes')::numeric,
    period_resets_at = (p_new->>'period_resets_at')::timestamptz
  where id = p.id;
  update public.orders set status = 'paid', razorpay_payment_id = coalesce(p_payment, razorpay_payment_id),
         paid_via = p_via, paid_at = now(), error = null
   where id = o.id;
  return 'ok';
end $$;

grant select on table public.plans to anon, authenticated;
grant select, insert, update, delete on table public.plans, public.orders to service_role;
grant select on table public.orders to authenticated;
revoke all on function public.commit_activation(uuid, text, text, jsonb, jsonb) from public, anon, authenticated;
grant execute on function public.commit_activation(uuid, text, text, jsonb, jsonb) to service_role;
