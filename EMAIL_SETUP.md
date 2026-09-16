# Admin bulk email setup

The Bulk Email panel is available only at `/admin`. It accepts `.csv` and
`.xlsx` recipient files, finds addresses in any column, removes duplicates, and
sends one email per recipient.

## Gmail OAuth configuration

Use the OAuth client JSON downloaded from Google Cloud. Keep it on the server;
do not upload it in the browser and do not commit it to Git.

1. In the same Google Cloud project, enable the **Gmail API**.
2. In **APIs & Services → Credentials**, create or use an **OAuth 2.0 Client ID**
   of type **Web application**.
3. Add this exact Authorized redirect URI, changing the domain and port to your
   running app:

   ```text
   http://localhost:3001/api/admin-email/oauth/callback
   ```

   For a deployed app, use HTTPS, for example:
   `https://your-domain.com/api/admin-email/oauth/callback`.
4. Add the following to `.env` (use the actual absolute location of your
   downloaded JSON):

   ```env
   GOOGLE_OAUTH_CLIENT_SECRETS=C:\\secure\\google-oauth-client.json
   GMAIL_OAUTH_REDIRECT_URI=http://localhost:3001/api/admin-email/oauth/callback
   # Optional. Leave this unset to send from the connected Gmail account.
   # GMAIL_SENDER_EMAIL=verified-alias@yourdomain.com
   ```

5. Install the updated dependencies and restart the app:

   ```powershell
   pip install -r requirements.txt
   ```
6. Sign in as `admin@v3.com`, open **Tenants**, select **Connect Gmail**, and
   approve the Gmail send permission using the mailbox that should send emails.

After approval, the refresh token is saved server-side as
`gmail-oauth-token.json` (or the file set in `GMAIL_OAUTH_TOKEN_FILE`). Protect
that file like a password. The current limits are 5 MB per upload and 500 unique
recipients per send.
