-- WisOrbit AI Video Agent - Phase 1 schema (run once in Supabase SQL editor)
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
