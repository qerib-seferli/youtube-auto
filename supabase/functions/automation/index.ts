import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "npm:@supabase/supabase-js@2";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, apikey, content-type",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
};

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { ...CORS, "Content-Type": "application/json" },
});

const b64 = (bytes: Uint8Array) => btoa(String.fromCharCode(...bytes))
  .replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");

const unb64 = (value: string) => Uint8Array.from(
  atob(value.replaceAll("-", "+").replaceAll("_", "/") + "=".repeat((4 - value.length % 4) % 4)),
  (char) => char.charCodeAt(0),
);

async function signState(payload: Record<string, unknown>) {
  const encoded = b64(new TextEncoder().encode(JSON.stringify(payload)));
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(Deno.env.get("OAUTH_STATE_SECRET")!),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(encoded)));
  return `${encoded}.${b64(signature)}`;
}

async function verifyState(token: string) {
  const [payloadRaw, signatureRaw] = token.split(".");
  if (!payloadRaw || !signatureRaw) throw new Error("Invalid OAuth state");
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(Deno.env.get("OAUTH_STATE_SECRET")!),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"],
  );
  const valid = await crypto.subtle.verify("HMAC", key, unb64(signatureRaw), new TextEncoder().encode(payloadRaw));
  if (!valid) throw new Error("Invalid OAuth state signature");
  const payload = JSON.parse(new TextDecoder().decode(unb64(payloadRaw)));
  if (Date.now() > payload.exp) throw new Error("OAuth state expired");
  return payload;
}

async function encryptToken(value: string) {
  const raw = Uint8Array.from(atob(Deno.env.get("TOKEN_ENCRYPTION_KEY")!), (c) => c.charCodeAt(0));
  if (raw.length !== 32) throw new Error("TOKEN_ENCRYPTION_KEY must decode to 32 bytes");
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const key = await crypto.subtle.importKey("raw", raw, "AES-GCM", false, ["encrypt"]);
  const encrypted = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, new TextEncoder().encode(value)));
  return `v1.${b64(iv)}.${b64(encrypted)}`;
}

async function runScheduler(admin: ReturnType<typeof createClient>) {
  const now = new Date();
  const monday = new Date(now);
  monday.setUTCDate(monday.getUTCDate() - ((monday.getUTCDay() + 6) % 7));
  monday.setUTCHours(0, 0, 0, 0);

  const { data: channels, error } = await admin
    .from("channels")
    .select("*")
    .eq("active", true)
    .eq("autopilot", true)
    .lte("next_run_at", now.toISOString())
    .order("priority")
    .limit(20);
  if (error) throw error;

  const created: string[] = [];
  for (const channel of channels ?? []) {
    const { data: week } = await admin
      .from("video_queue")
      .select("video_type")
      .eq("channel_id", channel.id)
      .gte("created_at", monday.toISOString())
      .neq("status", "cancelled");

    const longCount = (week ?? []).filter((v) => v.video_type === "long").length;
    const shortCount = (week ?? []).filter((v) => v.video_type === "short").length;
    const videoType = longCount < channel.weekly_long_target
      ? "long"
      : shortCount < channel.weekly_short_target ? "short" : null;

    if (videoType) {
      const { data: video, error: insertError } = await admin
        .from("video_queue")
        .insert({ owner_id: channel.owner_id, channel_id: channel.id, video_type: videoType, source: "autopilot" })
        .select("id")
        .single();
      if (insertError) throw insertError;
      created.push(video.id);
    }

    await admin.from("channels")
      .update({ next_run_at: new Date(Date.now() + 86_400_000).toISOString() })
      .eq("id", channel.id);
  }
  return { created };
}

Deno.serve(async (request) => {
  if (request.method === "OPTIONS") return new Response("ok", { headers: CORS });

  const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
  const admin = createClient(supabaseUrl, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!, { auth: { persistSession: false } });
  const url = new URL(request.url);

  try {
    if (request.method === "GET" && url.searchParams.get("code")) {
      const state = await verifyState(url.searchParams.get("state") || "");
      const redirectUri = `${supabaseUrl}/functions/v1/automation`;
      const tokenResponse = await fetch("https://oauth2.googleapis.com/token", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          code: url.searchParams.get("code")!,
          client_id: Deno.env.get("GOOGLE_CLIENT_ID")!,
          client_secret: Deno.env.get("GOOGLE_CLIENT_SECRET")!,
          redirect_uri: redirectUri,
          grant_type: "authorization_code",
        }),
      });
      const token = await tokenResponse.json();
      if (!tokenResponse.ok || !token.refresh_token) throw new Error(token.error_description || "Google refresh token missing");

      const channelResponse = await fetch("https://www.googleapis.com/youtube/v3/channels?part=id,snippet&mine=true", {
        headers: { Authorization: `Bearer ${token.access_token}` },
      });
      const channelData = await channelResponse.json();
      if (!channelResponse.ok || !channelData.items?.[0]) throw new Error("YouTube channel not found");

      await admin.from("channel_secrets").upsert({
        channel_id: state.channel_id,
        refresh_token_cipher: await encryptToken(token.refresh_token),
        scopes: String(token.scope || "").split(" ").filter(Boolean),
      });
      await admin.from("channels").update({
        youtube_channel_id: channelData.items[0].id,
        youtube_handle: channelData.items[0].snippet?.customUrl || null,
      }).eq("id", state.channel_id).eq("owner_id", state.owner_id);

      return Response.redirect(`${state.return_url}?youtube=connected`, 302);
    }

    const body = request.method === "POST" ? await request.json() : {};
    if (body.action === "scheduler") {
      if (request.headers.get("x-cron-secret") !== Deno.env.get("CRON_SECRET")) return json({ error: "Unauthorized" }, 401);
      return json(await runScheduler(admin));
    }

    const authorization = request.headers.get("Authorization");
    if (!authorization) return json({ error: "Unauthorized" }, 401);
    const userClient = createClient(supabaseUrl, Deno.env.get("SUPABASE_ANON_KEY")!, {
      global: { headers: { Authorization: authorization } }, auth: { persistSession: false },
    });
    const { data: { user } } = await userClient.auth.getUser();
    if (!user) return json({ error: "Invalid session" }, 401);

    if (body.action === "oauth_start") {
      const { data: channel } = await admin.from("channels").select("id").eq("id", body.channel_id).eq("owner_id", user.id).single();
      if (!channel) return json({ error: "Channel not found" }, 404);
      const state = await signState({ channel_id: channel.id, owner_id: user.id, return_url: body.return_url, exp: Date.now() + 600_000 });
      const params = new URLSearchParams({
        client_id: Deno.env.get("GOOGLE_CLIENT_ID")!,
        redirect_uri: `${supabaseUrl}/functions/v1/automation`,
        response_type: "code",
        access_type: "offline",
        prompt: "consent",
        scope: "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly",
        state,
      });
      return json({ url: `https://accounts.google.com/o/oauth2/v2/auth?${params}` });
    }

    return json({ error: "Unknown action" }, 400);
  } catch (error) {
    console.error(error);
    return json({ error: error instanceof Error ? error.message : "Server error" }, 500);
  }
});
