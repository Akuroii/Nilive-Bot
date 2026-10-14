# Nilive Bot — Startup / Offline Debug Guide (Railway)

Use this if the bot shows as offline in Discord or the Railway deploy
looks crashed/restarting.

## 1. Enable Privileged Intents (Discord Developer Portal)
The bot requests all intents (`discord.Intents.all()`), which requires
these to be turned ON in the portal or Discord will refuse the connection:

1. Go to https://discord.com/developers/applications
2. Select your application → **Bot** (left sidebar)
3. Scroll to **Privileged Gateway Intents**
4. Enable all three:
   - Presence Intent
   - Server Members Intent
   - Message Content Intent
5. Click **Save Changes**

## 2. Verify DISCORD_TOKEN is set in Railway
1. Open your Railway project → select the bot service
2. Go to the **Variables** tab
3. Confirm a variable named exactly `DISCORD_TOKEN` exists and has a value
   - No quotes around it, no trailing spaces
   - If you regenerated the token in the Developer Portal, paste the new
     one here and it must match exactly
4. Also confirm these exist if you use them:
   - `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DISCORD_REDIRECT_URI`
     (dashboard OAuth)
   - `OWNER_ID` (defaults to a hardcoded fallback if unset)
   - `PORT` (Railway sets this automatically — don't override it)

## 3. Read the Railway logs
1. Railway project → service → **Deployments** → click the active deploy
2. Open **Logs**
3. Look near the top of the log for one of these (added by this patch):
   - `FATAL: DISCORD_TOKEN is missing or empty.` → variable isn't set,
     go back to step 2
   - `WARNING: DISCORD_TOKEN is set but doesn't look like a valid
     Discord bot token` → you likely pasted the wrong value (client
     secret instead of bot token, or a truncated/quoted paste)
   - `FATAL: Discord rejected DISCORD_TOKEN (LoginFailure)` → the token
     is set but Discord itself rejected it (revoked/regenerated) —
     grab a fresh token from the Developer Portal
   - `Token check: OK` and `Intents status: ...` → token and intents
     passed local checks; if it's still offline after this line, check
     Discord's status page or your intents settings (step 1)
4. If cogs fail to load, you'll see `Failed to load cogs.X: <error>` —
   that's a code error in a specific cog, not a token/connection issue.

## 4. Restart and verify
1. Railway → service → **Deployments** → **Redeploy** (or push a new
   commit if you just changed an env var — env var changes alone
   usually trigger an automatic redeploy)
2. Watch the logs for `Nero is online as <bot name>`
3. In Discord, confirm the bot's status dot is green/online in your
   server's member list

## Still stuck?
- Double-check the bot was actually invited to the server with the
  `bot` + `applications.commands` OAuth scopes.
- Check Railway's **Metrics** tab for repeated crash/restart loops —
  that usually means an exception during `cogs` loading or database
  init, visible in the logs from step 3.
