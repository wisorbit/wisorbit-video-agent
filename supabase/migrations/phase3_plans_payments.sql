-- ============ Phase 3: plans + payments (Razorpay prepaid passes) ============
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
