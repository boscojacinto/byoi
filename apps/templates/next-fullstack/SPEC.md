# Acceptance spec (paste into the brief)

- `GET /api/health` returns 200 with `ok: true` and reports `database`, `cache`, and `auth` all `ok`.
- `GET /api/notes` without a session cookie returns 401.
- `POST /api/auth` with `{"user":"ada"}` returns 200 and sets a `byoi_session` cookie.
- `POST /api/notes` with a valid session and `{"body":"first note"}` returns 201 and echoes the stored note.
- `POST /api/notes` with a valid session and an empty body returns 400.
- `GET /api/notes` with a valid session returns the notes that user created, and nobody else's.
- A second `GET /api/notes` within 30 seconds is served from cache (`cached: true`).
