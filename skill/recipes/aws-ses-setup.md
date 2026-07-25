# Recipe: first-time AWS SES setup

Run once per museletter install. Needs the `aws` CLI with credentials for the
account that will send. Substitute the user's domain and server URL throughout.

1. **Verify the sending domain** (creates DKIM tokens):
   ```bash
   aws sesv2 create-email-identity --email-identity example.com
   aws sesv2 get-email-identity --email-identity example.com \
     --query 'DkimAttributes.Tokens'
   ```
   Have the user add each token as a CNAME:
   `<token>._domainkey.example.com → <token>.dkim.amazonses.com`

2. **Recommend DMARC** if missing: TXT record `_dmarc.example.com` with value
   `v=DMARC1; p=none` (Gmail/Yahoo require DMARC for bulk senders).

3. **Wire bounce/complaint events back to museletter** (critical — without this,
   bounces are never suppressed and the SES account reputation degrades):
   ```bash
   aws sesv2 create-configuration-set --configuration-set-name museletter
   aws sns create-topic --name museletter-events   # note the TopicArn
   aws sesv2 create-configuration-set-event-destination \
     --configuration-set-name museletter \
     --event-destination-name museletter-sns \
     --event-destination '{"Enabled":true,"MatchingEventTypes":["BOUNCE","COMPLAINT","DELIVERY"],"SnsDestination":{"TopicArn":"<TopicArn>"}}'
   aws sns subscribe --topic-arn <TopicArn> --protocol https \
     --notification-endpoint https://<museletter-server>/webhooks/sns
   ```
   museletter confirms the SNS subscription automatically. Then set
   `MUSELETTER_SES_CONFIGURATION_SET=museletter` and
   `MUSELETTER_SNS_TOPIC_ARN=<TopicArn>` on the server and restart it. Setting
   the topic ARN is important: without it the webhook trusts any validly-signed
   SNS message, so an attacker could forge bounce events from their own topic
   and suppress your subscribers.

4. **Request production access** (new SES accounts are sandboxed to verified
   addresses, 200 emails/day). This is a support form in the SES console —
   the user must do it; tell them to describe their newsletter honestly and
   mention double opt-in and automatic bounce/complaint suppression.

5. **Minimal IAM policy** for the server's credentials:
   `ses:SendEmail`, `ses:GetAccount`, `ses:GetEmailIdentity`.

6. Verify everything: `museletter doctor` should show no failures
   (sandbox shows as a failure until step 4 is approved).
