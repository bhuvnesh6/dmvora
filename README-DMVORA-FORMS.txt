DMvora Forms Studio update

Files:
- app.py: replace your current project app.py with this updated backend. Existing routes are retained; adds DB migrations and native Meta Flow create/upload/publish APIs.
- app.html: replace templates/app.html. Loads forms-studio.js and replaces the manual Flow-ID form with the visual form designer.
- forms-studio.js: copy to static/forms-studio.js.
- forms-studio.css: append its contents to static/style.css (or link it from app.html).

Important setup:
1. Keep your existing .env, SQLite database and connected Meta number tokens. The DB migration is automatic.
2. The Meta access token for the selected number must have WhatsApp Business Management permissions allowing Flow creation/management (commonly whatsapp_business_management) and messaging permission for sending.
3. Create a draft Flow, then click Publish in Forms Studio. Meta publishing is irreversible for that Flow; edits require a new Flow version.
4. This initial builder supports text, email, phone, number, date, dropdown, single-choice and multi-choice fields in a single screen. It intentionally does not expose unsupported components or multi-screen branching yet.
5. Existing saved Meta Flow forms remain visible. Existing form submissions continue through your current nfm_reply webhook path.
6. Test in a non-production WABA first. The Meta asset upload response contains validation_errors; DMvora will not publish a Flow if validation fails.

Copy: app.py -> project/app.py; app.html -> project/templates/app.html; forms-studio.js -> project/static/forms-studio.js; append forms-studio.css -> project/static/style.css.
