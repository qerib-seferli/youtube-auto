begin;
create extension if not exists pgcrypto;
create extension if not exists pg_cron;
create extension if not exists pg_net;

create table public.channels(
 id uuid primary key default gen_random_uuid(),owner_id uuid not null references auth.users(id) on delete cascade,
 name text not null,language text not null default 'en',niche text not null default 'motivation',custom_prompt text,
 audience_type text not null default 'general' check(audience_type in('general','kids')),made_for_kids boolean not null default false,
 timezone text not null default 'America/New_York',weekly_long_target smallint not null default 2,weekly_short_target smallint not null default 3,
 long_min_minutes smallint not null default 7,long_max_minutes smallint not null default 10,short_min_seconds smallint not null default 35,short_max_seconds smallint not null default 55,
 voice_provider text not null default 'edge_tts',voice_id text not null default 'en-US-AriaNeural',privacy_status text not null default 'private' check(privacy_status in('private','unlisted','public')),
 priority smallint not null default 50,autopilot boolean not null default true,active boolean not null default true,
 youtube_channel_id text,youtube_handle text,next_run_at timestamptz default now(),last_uploaded_at timestamptz,created_at timestamptz default now(),updated_at timestamptz default now(),unique(owner_id,name)
);
create table public.channel_secrets(channel_id uuid primary key references public.channels(id) on delete cascade,refresh_token_cipher text not null,scopes text[] default '{}',connected_at timestamptz default now());
create table public.video_queue(
 id uuid primary key default gen_random_uuid(),owner_id uuid not null references auth.users(id) on delete cascade,channel_id uuid not null references public.channels(id) on delete cascade,
 video_type text not null check(video_type in('long','short')),source text not null default 'autopilot',status text not null default 'pending' check(status in('pending','generating','rendering','ready','uploading','uploaded','failed','cancelled')),
 topic text,title text,description text,tags text[] default '{}',hashtags text[] default '{}',script text,scene_plan jsonb default '[]',thumbnail_text text,
 audio_path text,subtitle_path text,thumbnail_path text,video_path text,duration_seconds numeric,publish_at timestamptz,youtube_video_id text unique,youtube_url text,
 attempt_count smallint default 0,max_attempts smallint default 3,locked_by text,locked_at timestamptz,heartbeat_at timestamptz,error_message text,created_at timestamptz default now(),updated_at timestamptz default now()
);
create table public.media_history(id bigint generated always as identity primary key,channel_id uuid references public.channels(id) on delete cascade,video_id uuid references public.video_queue(id) on delete cascade,provider text,provider_asset_id text,source_url text,used_at timestamptz default now());
create table public.topic_history(id bigint generated always as identity primary key,channel_id uuid references public.channels(id) on delete cascade,video_id uuid references public.video_queue(id) on delete cascade,topic text,fingerprint text,used_at timestamptz default now());
create table public.events(id bigint generated always as identity primary key,owner_id uuid references auth.users(id) on delete cascade,video_id uuid references public.video_queue(id) on delete cascade,channel_id uuid references public.channels(id) on delete cascade,level text default 'info',type text,message text,payload jsonb default '{}',created_at timestamptz default now());
create table public.worker_state(worker_id text primary key,last_seen_at timestamptz default now(),current_video_id uuid references public.video_queue(id),version text);

create index on public.video_queue(status,publish_at,created_at);
create index on public.channels(active,autopilot,next_run_at,priority);

create or replace function public.touch() returns trigger language plpgsql as $$begin new.updated_at=now();return new;end$$;
create trigger channels_touch before update on public.channels for each row execute function public.touch();
create trigger videos_touch before update on public.video_queue for each row execute function public.touch();

create or replace function public.video_event() returns trigger language plpgsql security definer as $$begin insert into public.events(owner_id,video_id,channel_id,type,message) values(new.owner_id,new.id,new.channel_id,'video_created','Video növbəyə əlavə edildi');return new;end$$;
create trigger video_created after insert on public.video_queue for each row execute function public.video_event();

create or replace function public.claim_next_video(p_worker_id text) returns setof public.video_queue language plpgsql security definer as $$
declare x uuid;begin
 select id into x from public.video_queue where ((status='pending') or (status='ready' and coalesce(publish_at,now())<=now())) and attempt_count<max_attempts and locked_at is null order by case when status='ready' then 0 else 1 end,created_at for update skip locked limit 1;
 if x is null then return;end if;
 update public.video_queue set locked_by=p_worker_id,locked_at=now(),heartbeat_at=now(),attempt_count=attempt_count+1 where id=x;
 return query select * from public.video_queue where id=x;
end$$;
revoke all on function public.claim_next_video(text) from public,anon,authenticated;grant execute on function public.claim_next_video(text) to service_role;

alter table public.channels enable row level security;alter table public.channel_secrets enable row level security;alter table public.video_queue enable row level security;alter table public.media_history enable row level security;alter table public.topic_history enable row level security;alter table public.events enable row level security;alter table public.worker_state enable row level security;
create policy "channels read" on public.channels for select to authenticated using(owner_id=auth.uid());
create policy "channels insert" on public.channels for insert to authenticated with check(owner_id=auth.uid());
create policy "channels update" on public.channels for update to authenticated using(owner_id=auth.uid()) with check(owner_id=auth.uid());
create policy "channels delete" on public.channels for delete to authenticated using(owner_id=auth.uid());
create policy "videos read" on public.video_queue for select to authenticated using(owner_id=auth.uid());
create policy "videos insert" on public.video_queue for insert to authenticated with check(owner_id=auth.uid());
create policy "events read" on public.events for select to authenticated using(owner_id=auth.uid());

insert into storage.buckets(id,name,public) values('audio','audio',false),('renders','renders',false),('thumbnails','thumbnails',false) on conflict do nothing;
alter publication supabase_realtime add table public.video_queue;
commit;
