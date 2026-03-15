app.secret_key = os.getenv("SECRET_KEY", "nero-dashboard-secret-key-2024")
```

The problem is `os.urandom(24)` generates a new secret key every time the server restarts — so your session gets wiped and you keep getting logged out in a loop!

**Step 4 — Go to Railway → Variables → add:**
```
SECRET_KEY = nero-dashboard-secret-2024
